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

from nador.config import (
    DeployedMonitoringComponent,
    MonitoringDeployment,
    MonitoringModel,
    ResolvedTarget,
    validate_deployment,
)
from nador.planner import ASSETS, resolve_components, resolve_targets


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
        if monitoring.metrics.throughput and not monitoring.targets:
            raise ValueError("Throughput requires AMF, SMF and UPF packet targets")
        node_name, _ = self._get_deployment_node(None)
        model = self.deployment_model.model_copy(update={"monitoring": monitoring})
        targets = resolve_targets(model)
        monitoring.components = resolve_components(model, targets)
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

    async def set_running(
        self,
        *,
        n3_one_way: bool | None = None,
        upf_transit: bool | None = None,
        n3_log_samples: bool | None = None,
        quiet: bool = False,
    ) -> None:
        """Start or stop deployed one-way metrics on the running controller, without reinstalling."""
        metrics = self.deployment_model.monitoring.metrics
        requested = {"n3_one_way": ("n3", n3_one_way), "upf_transit": ("upf_transit", upf_transit)}
        changes: dict[str, dict] = {}
        for name, (_key, running) in requested.items():
            if running is not None:
                changes.setdefault(name, {})["enabled"] = running
        if n3_log_samples is not None:
            changes.setdefault("n3_one_way", {})["log_samples"] = n3_log_samples
        for name in changes:
            if not metrics.available(name):
                raise ValueError(f"Deploy with metrics.{name} (on_demand: true starts it stopped)")
        if not changes:
            return
        await self._run_tasks(
            "patch_controller_settings",
            settings={requested[name][0]: patch for name, patch in changes.items()},
            quiet=quiet,
        )
        for name, patch in changes.items():
            update = {"log_samples": patch["log_samples"]} if "log_samples" in patch else {}
            if "enabled" in patch:
                update["on_demand"] = not patch["enabled"]
            if (metric := metrics.get(name)) is not None:
                setattr(metrics, name, metric.model_copy(update=update))

    async def deploy(self, *, quiet: bool = False) -> None:
        """Deploy monitoring and wait for its components and sniffers."""
        plan = self.plan()
        await self._run_tasks("prepare", quiet=quiet)
        await self.install(quiet=quiet)
        monitoring = self.deployment_model.monitoring
        if monitoring.needs_session_stack:
            # Start capture before UEs attach, so the mapper sees session establishment.
            await self._run_tasks("inject_sniffers", targets=plan.targets, quiet=quiet)

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
