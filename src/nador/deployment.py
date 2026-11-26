"""Monitoring deployment lifecycle and workload logs."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self

from ifran.deployment.base import BaseManagement
from ifran.deployment.helm import HelmManagement
from ifran.models.deployment import DeploymentModel
from ifran.models.provisioning import ProvisionableNode
from ifran.utils.ansible import get_ansible_inventory_line, run_ansible_role
from metamodel_3gpp.models.metamodel import MetaModel
from pydantic import ValidationError

from nador.config import (
    CONTROLLER_METRICS,
    GROUP_OF,
    DeployedMonitoringComponent,
    Metrics,
    MonitoringDeployment,
    MonitoringModel,
    ResolvedTarget,
    metric_path,
    profile_key,
    validate_deployment,
)
from nador.planner import ASSETS, ocudu_dus, resolve_components, resolve_targets, ue_names
from nador.probes import HOST_PROBES, kind_of


@dataclass(frozen=True)
class Plan:
    """What deploy() will install: the control node, the located targets and every Helm release."""

    control_node: str
    targets: list[ResolvedTarget]
    components: dict[str, DeployedMonitoringComponent]


def _sniffer_settings(monitoring: MonitoringDeployment) -> dict:
    """What the AMF/SMF sniffers run and export, for the inject_sniffers role."""
    metrics = monitoring.metrics
    return {
        "amf": {
            "image": monitoring.image("amf_sniffer"),
            "metrics_enabled": metrics.running("ngap"),
            "metrics_port": metrics.ngap.port if metrics.ngap else 9102,
        },
        "smf": {
            "image": monitoring.image("smf_sniffer"),
            "metrics_enabled": metrics.running("pfcp"),
            "metrics_port": metrics.pfcp.port if metrics.pfcp else 9104,
        },
        "pending_timeout_seconds": monitoring.tuning.sniffer_pending_timeout_seconds,
    }


class MonitoringManagement(HelmManagement):
    """Deploy monitoring components and attachment sniffers."""

    def __init__(self, preceding: BaseManagement, *, config: MonitoringDeployment | str | Path | None = None) -> None:
        super().__init__(preceding=preceding)
        self.deployment_model = MonitoringModel.model_validate(self.deployment_model.model_dump(mode="python"))
        if isinstance(config, (str, Path)):
            config = MonitoringDeployment.from_yaml_file(config)
        if config is not None:
            self.deployment_model.monitoring = config.model_copy(deep=True)

    @classmethod
    def from_yaml_files(cls, metamodel_file: str | Path, deployment_model_file: str | Path) -> Self:
        """Restore an exported monitoring deployment without contacting its hosts."""
        model = MonitoringModel.from_yaml_file(str(deployment_model_file))
        base = BaseManagement(
            metamodel=MetaModel.from_yaml_file(str(metamodel_file)),
            deployment_model=DeploymentModel.model_validate(model.model_dump(exclude={"monitoring"})),
        )
        return cls(preceding=base, config=model.monitoring)

    def plan(self) -> Plan:
        """Resolve targets and chart values without contacting deployment hosts."""
        monitoring = validate_deployment(self.deployment_model.monitoring.model_dump(mode="python"))
        node_name, _ = self._get_deployment_node(None)
        model = self.deployment_model.model_copy(update={"monitoring": monitoring})
        targets = resolve_targets(model)
        monitoring.components = resolve_components(
            model, targets, ue_names(self.metamodel), ocudu_dus(self.metamodel, model)
        )
        self.deployment_model.monitoring = monitoring
        return Plan(control_node=node_name, targets=targets, components=monitoring.components)

    async def prepare(self, *, quiet: bool = False) -> None:
        """Resolve chart values, label the monitoring node and add Helm repositories."""
        self.plan()
        await self._run_tasks("prepare", quiet=quiet)

    async def run_action(
        self,
        action: str,
        *,
        components: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, str | dict[str, str]]:
        """Run a Helm action in installation order, or in reverse for uninstall."""
        if action in ("prepare", "install"):
            validate_deployment(self.deployment_model.monitoring.model_dump(mode="python"))
        available = self.deployment_model.monitoring.components
        names = available if components is None else components
        unknown = set(names) - available.keys()
        if unknown:
            raise ValueError(f"Unknown monitoring components: {sorted(unknown)}")
        selected = [(name, available[name]) for name in names]
        if components is None and action in ("get_logs", "run_cmd"):
            selected = [
                (name, component) for name, component in selected if component.is_installed and component.pod_selector
            ]
        if action == "uninstall":
            selected.reverse()
        node = self._get_deployment_node(None)
        output = {}
        for name, component in selected:
            result = await _run_component_action(
                model=self.deployment_model,
                component=component,
                component_name=name,
                node=node,
                action=action,
                **kwargs,
            )
            if result is not None:
                output[name] = result
        return output

    async def _run_tasks(
        self,
        action: Literal["prepare", "inject_sniffers", "patch_controller_settings"],
        *,
        targets: list[ResolvedTarget] | None = None,
        settings: dict | None = None,
        quiet: bool = False,
    ) -> None:
        """Run a monitoring stack task on the Kubernetes control node."""
        monitoring = self.deployment_model.monitoring
        node_name, control_node = self._get_deployment_node(None)
        await run_ansible_role(
            role="monitoring/stack",
            hosts=node_name,
            inventory=get_ansible_inventory_line(node_name, control_node),
            become=True,
            inputs={
                "monitoring_action": action,
                "monitoring_control_node": node_name,
                "monitoring_config": monitoring.model_dump(mode="json", exclude={"components"}),
                "monitoring_targets": [target.values() for target in targets or []],
                "sniffers": _sniffer_settings(monitoring),
                "controller_settings": settings,
            },
            extra_params={"name": "Monitoring"},
            runner_path=str(Path(self.deployment_model.params.ansible.runner_path) / "monitoring"),
            roles_paths=[str(ASSETS / "roles"), *self.deployment_model.params.ansible.roles_paths],
            quiet=quiet,
        )

    async def start(
        self,
        metric: str,
        *,
        imsis: list[str] | None = None,
        slices: list[str] | None = None,
        at: list[str] | None = None,
        log_samples: bool | None = None,
        quiet: bool = False,
    ) -> None:
        """Start a deployed controller-managed metric, or change its scope, without reinstalling.

        ``imsis`` and ``slices`` narrow ``rtt`` and ``throughput`` to some UEs (an empty list clears
        the filter); ``at`` moves ``rtt`` to ``["gnb"]``, ``["upf"]`` or both; ``log_samples``
        applies to ``n3_one_way``. Host-probe metrics start and stop
        with their probe, so ``kernel_drops`` and ``attributed_drops`` move together.
        ``tcp_flows`` and ``attributed_drops`` only collect when they were deployed running.
        """
        options = {"imsis": imsis, "slices": slices, "at": at, "log_samples": log_samples}
        await self._set_running(metric, True, options, quiet)

    async def stop(self, metric: str, *, quiet: bool = False) -> None:
        """Stop a controller-managed metric; ``start`` resumes it."""
        await self._set_running(metric, False, {}, quiet)

    async def _set_running(self, metric: str, running: bool, options: dict[str, Any], quiet: bool) -> None:
        """Merge a start or stop into the controller's settings; it reconciles within seconds."""
        metrics = self.deployment_model.monitoring.metrics
        if metric not in CONTROLLER_METRICS and metric not in HOST_PROBES:
            raise ValueError(f"{metric} is not started or stopped by the controller")
        if not metrics.available(metric):
            raise ValueError(f"Deploy with {metric_path(metric)} (on_demand: true starts it stopped)")
        allowed = {"rtt": {"imsis", "slices", "at"}, "throughput": {"imsis", "slices"}, "n3_one_way": {"log_samples"}}
        extra = {key for key, value in options.items() if value is not None} - allowed.get(metric, set())
        if extra:
            raise ValueError(f"{metric} does not take {', '.join(sorted(extra))}")
        if running:
            self._check_can_start(metric)
        given = {k: v for k, v in options.items() if v is not None}
        moved, settings = self._controller_change(metric, running, given)
        await self._run_tasks("patch_controller_settings", settings=settings, quiet=quiet)
        # Keep the in-memory profile in step, so plan() and later saves show what runs.
        for name in moved:
            current = metrics.get(name)
            if current is None:
                continue
            update: dict[str, Any] = {"on_demand": not running}
            if name == metric:
                update |= {k: v for k, v in given.items() if k in type(current).model_fields}
            setattr(metrics, name, current.model_copy(update=update))

    def _controller_change(self, metric: str, running: bool, given: dict[str, Any]) -> tuple[list[str], dict]:
        """The metrics a start or stop moves, and the partial settings the controller reads."""
        metrics = self.deployment_model.monitoring.metrics
        if metric in HOST_PROBES:
            kind = kind_of(metric)
            moved = [name for name in HOST_PROBES if kind_of(name) == kind and metrics.available(name)]
            return moved, {"host_probes": {kind: running}}
        current = metrics.get(metric)
        if current is not None and given:
            # Same checks as the profile: IMSI and slice formats, packet pairing needs both anchors.
            try:
                type(current).model_validate(current.model_dump() | given)
            except ValidationError as exc:
                problems = "; ".join(
                    ": ".join([*(str(part) for part in e["loc"][:1]), e["msg"].removeprefix("Value error, ")])
                    for e in exc.errors()
                )
                raise ValueError(f"{metric}: {problems}") from None
        change: dict[str, Any] = {"enabled": running} | given
        if "at" in change:
            change["targets"] = self._rtt_anchors(change.pop("at"))
        return [metric], {metric: change}

    def _rtt_anchors(self, at: list[str]) -> list[dict[str, Any]]:
        """The gNB/UPF targets RTT probes should run on, as the controller's target list."""
        anchors = [target.values() for target in resolve_targets(self.deployment_model) if target.role in at]
        missing = set(at) - {anchor["role"] for anchor in anchors}
        if missing:
            raise ValueError(f"rtt cannot measure at {', '.join(sorted(missing))}: no target has that role")
        return anchors

    def _check_can_start(self, metric: str) -> None:
        """Refuse a start the deployed controller settings cannot run: a missing probe, target or image."""
        controller = self.deployment_model.monitoring.components.get("monitoring-controller")
        if controller is None:
            raise ValueError("Deploy monitoring first: the controller is not part of this deployment")
        values = controller.chart_values
        if metric in HOST_PROBES:
            if kind_of(metric) not in values.get("hostProbeStates", {}):
                raise ValueError(f"{metric}: its probe is not deployed for this RAN")
            return
        chart_keys = {"rtt": "rtt", "throughput": "throughput", "user_plane_paths": "userPlanePaths"}
        if metric in chart_keys:
            if not values.get(chart_keys[metric], {}).get("allowed"):
                raise ValueError(f"{metric} needs gNB/UPF targets, AMF/SMF session context and its probe image")
            return
        one_way = values.get("oneWay", {})
        if not one_way.get("image"):
            raise ValueError(f"Deploy with {metric_path(metric)} first")
        if metric == "upf_transit" and not (one_way["upf_transit"].get("peer") and one_way.get("test_port")):
            raise ValueError("upf_transit needs its peer and test_port")

    async def deploy(self, *, quiet: bool = False) -> None:
        """Deploy monitoring and wait for its components and sniffers."""
        plan = self.plan()
        await self._run_tasks("prepare", quiet=quiet)
        await self.install(quiet=quiet)
        monitoring = self.deployment_model.monitoring
        if monitoring.needs_session_stack:
            # Start capture before UEs attach, so the mapper sees session establishment.
            await self._run_tasks("inject_sniffers", targets=plan.targets, quiet=quiet)

    async def deploy_probe(self, metric: str, options: dict[str, Any] | bool = True, *, quiet: bool = False) -> None:
        """Deploy one metric on a running deployment, or redeploy it with new options.

        ``options`` are written as in ``monitoring.yaml``: every option of the metric, or ``True``
        for a metric without options. Only the releases this changes are installed or upgraded,
        and the AMF/SMF sniffers start if this is the first metric that needs them; UEs that
        attached before that are only seen once they reattach.
        """
        if options is False:
            raise ValueError("Use remove_probe to undeploy a metric")
        await self._redeploy_with(metric, options, quiet)

    async def remove_probe(self, metric: str, *, quiet: bool = False) -> None:
        """Undeploy one metric, uninstalling the releases nothing else needs."""
        if not self.deployment_model.monitoring.metrics.available(metric):
            raise ValueError(f"{metric} is not deployed")
        await self._redeploy_with(metric, False, quiet)

    async def _redeploy_with(self, metric: str, value: dict[str, Any] | bool, quiet: bool) -> None:
        if metric not in Metrics.model_fields:
            raise ValueError(f"Unknown metric {metric!r}")
        previous = self.deployment_model.monitoring
        before = previous.components
        had_sniffers = previous.needs_session_stack
        data = previous.model_dump(mode="python")
        data["metrics"][GROUP_OF[metric]][profile_key(metric)] = value
        self.deployment_model.monitoring = validate_deployment(data, explicit=True)
        try:
            plan = self.plan()
        except ValueError:
            self.deployment_model.monitoring = previous
            raise
        try:
            await self._change_releases(before, plan.components, quiet)
        except BaseException:
            # Back to the previous profile, keeping what did change: releases uninstalled are marked so (the
            # uninstall updated them in place), and new releases already installed are kept so remove() finds them.
            for name, component in plan.components.items():
                if name not in before and component.is_installed:
                    previous.components[name] = component
            self.deployment_model.monitoring = previous
            raise
        if self.deployment_model.monitoring.needs_session_stack and not had_sniffers:
            await self._run_tasks("inject_sniffers", targets=plan.targets, quiet=quiet)

    async def _change_releases(
        self, before: dict[str, DeployedMonitoringComponent], after: dict[str, DeployedMonitoringComponent], quiet: bool
    ) -> None:
        """Uninstall the releases no longer planned, then install the new and changed ones."""

        def same(name: str) -> bool:
            old, new = before[name], after[name]
            return old.is_installed and old.model_dump(exclude={"is_installed"}) == new.model_dump(
                exclude={"is_installed"}
            )

        for name in after.keys() & before.keys():
            after[name].is_installed = before[name].is_installed
        gone = [name for name in before if name not in after and before[name].is_installed]
        changed = [name for name in after if name not in before or not same(name)]
        node = self._get_deployment_node(None)
        for name in reversed(gone):
            await _run_component_action(
                model=self.deployment_model,
                component=before[name],
                component_name=name,
                node=node,
                action="uninstall",
                quiet=quiet,
            )
        if changed:
            await self.install(components=changed, quiet=quiet)

    async def remove(self, *, quiet: bool = False) -> None:
        """Remove monitoring using the current deployment configuration."""
        await self.uninstall(quiet=quiet)


async def _run_component_action(
    *,
    model: MonitoringModel,
    component: DeployedMonitoringComponent,
    component_name: str,
    node: tuple[str, ProvisionableNode],
    action: str,
    command: list[str] | None = None,
    quiet: bool = False,
    containers: list[str] | None = None,
    since_seconds: int = 3600,
) -> str | dict[str, str] | None:
    """Run a component action and record whether its Helm release is installed."""
    monitoring = model.monitoring
    component.namespace = monitoring.namespace
    ansible = model.params.ansible
    node_name, node_data = node
    if action in ("get_logs", "run_cmd") and not component.pod_selector:
        raise ValueError(f"Monitoring component {component_name!r} has no workload")
    if action == "run_cmd" and not command:
        raise ValueError("A command is required for run_cmd")

    output_key = {
        "get_logs": "logs",
        "run_cmd": "command_output",
    }.get(action)
    result = await run_ansible_role(
        role="monitoring/component",
        hosts=node_name,
        inventory=get_ansible_inventory_line(node_name, node_data),
        become=True,
        inputs={
            "helm_action": action,
            "component": component.model_dump(mode="json", exclude_none=True),
            "chart_name": component.release_name or component_name,
            "kubeconfig": monitoring.kubeconfig,
            "timeout_seconds": monitoring.operation_timeout_seconds,
            "since_seconds": since_seconds,
            "containers": containers or [],
            "command": command,
        },
        output_keys=[output_key] if output_key else None,
        runner_path=f"{ansible.runner_path}/monitoring/{component_name}/{action}",
        roles_paths=[str(ASSETS / "roles"), *ansible.roles_paths],
        quiet=quiet,
    )

    if action in ("install", "uninstall"):
        component.is_installed = action == "install"
    elif output_key:
        if result is None:
            raise RuntimeError(f"{component_name}: {action} returned no output")
        return result[output_key]
    return None
