"""Resolve model components and prepare monitoring Helm values."""

from pathlib import Path

import yaml
from ifran.models.provisioning import ProvisionableNode
from ifran.utils.ansible import get_ansible_inventory_line, run_ansible_role
from ifran.utils.k8s import build_k8s_safe
from ifran.utils.utils import deep_merge
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from nador.controller_values import controller_values
from nador.images import split_image
from nador.models import DeployedMonitoringComponent, MonitoringModel, component_image_defaults
from nador.probe_catalog import (
    NOT_ENABLED_WHILE_PAUSED,
    PROBES,
    SPLIT_SIGNALING,
    controlled_probes,
    enabled_probes,
    probe_kind,
    probe_values,
    release_name,
    workload,
)
from nador.ran_metrics import ocudu_sources

ASSETS = Path(__file__).resolve().parent / "assets"
CHARTS = ASSETS / "charts"
REMOTE_CHARTS = "/tmp/monitoring-charts"  # noqa: S108 - written as root on the control node by the prepare role
VALUES_TEMPLATES = Environment(  # noqa: S701 - renders YAML, not HTML; values go through tojson
    loader=FileSystemLoader(ASSETS / "templates"),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def _probe_states(model):
    if not model.monitoring.probe_controller_image:
        return {}
    states = controlled_probes(model.monitoring.extended)
    kinds = {str(nf.type) for nf in model.ran.components.values()}
    if not (("du" in kinds and kinds.intersection({"cu", "cu_cp", "cu_up"})) or {"cu_cp", "cu_up"} <= kinds):
        if states.get(SPLIT_SIGNALING):
            raise ValueError("split_signaling requires a modeled F1 or E1 split")
        states.pop(SPLIT_SIGNALING, None)
    return states


def resolve_targets(model: MonitoringModel) -> list[dict]:
    """Resolve stable selectors from the same release naming used by HelmManagement."""
    result = []
    for target in model.monitoring.targets:
        components = model.ues if target.category == "ue" else getattr(model, target.category).components
        if target.component not in components:
            raise ValueError(f"Unknown {target.category} component {target.component!r}")
        component = components[target.component]
        namespace = getattr(component, "namespace", None)
        if not namespace:
            raise ValueError(f"{target.component}: namespace is unresolved; pass the latest deployed manager")
        if target.role:
            expected = {"amf": {"amf"}, "smf": {"smf"}, "upf": {"upf"}, "gnb": {"gnb", "cu", "cu_up"}}
            if component.type not in expected[target.role]:
                raise ValueError(f"{target.component}: {component.type} cannot be a {target.role} packet target")
        selector = target.selector or {"app": build_k8s_safe(f"{component.implementation}-{target.component}")}
        result.append(
            target.model_dump()
            | {
                "namespace": namespace,
                "implementation": str(component.implementation),
                "selector": selector,
                "label_selectors": [f"{key}={value}" for key, value in selector.items()],
            }
        )
    return result


def _extended_chart_context(model, targets, probe_name, probe_states):
    settings = model.monitoring.extended
    extended_targets = [
        target | {"kind": str(getattr(model, target["category"]).components[target["component"]].type)}
        for target in targets
        if target["category"] in ("ran", "core")
    ]
    existing = {(target["category"], target["component"]) for target in extended_targets}
    for category in ("ran", "core"):
        for name, nf in getattr(model, category).components.items():
            if (category, name) not in existing and nf.namespace:
                extended_targets.append(
                    {
                        "category": category,
                        "component": name,
                        "namespace": nf.namespace,
                        "kind": str(nf.type),
                        "selector": {"app": build_k8s_safe(f"{nf.implementation}-{name}")},
                    }
                )
    selected = settings.probe_targets.get(probe_name)
    if selected:
        available = {f"{t['category']}/{t['component']}" for t in extended_targets}
        if set(selected) - available:
            raise ValueError(f"Unresolved probe targets for {probe_name}: {sorted(set(selected) - available)}")
        extended_targets = [t for t in extended_targets if f"{t['category']}/{t['component']}" in selected]
    extended_config = probe_values(settings, probe_name)
    if probe_name in probe_states:
        extended_config["paused"] = not probe_states[probe_name]
        if not probe_states[probe_name]:
            for flag in set(PROBES[probe_name]) - NOT_ENABLED_WHILE_PAUSED:
                extended_config[flag] = True
    return {
        "extended_targets": extended_targets,
        "extended_config": extended_config,
        "probe_image": settings.probe_images.get(probe_name),
    }


def _render_chart_values(
    component: DeployedMonitoringComponent,
    model: MonitoringModel,
    targets: list[dict],
    probe_states: dict,
) -> dict:
    """Render chart configuration before applying explicit user overrides."""
    chart = component.chart_ref.rsplit("/", 1)[-1]
    defaults = ASSETS / "defaults" / f"{chart}.yaml" if component.chart_version else CHARTS / chart / "values.yaml"
    node_selector = component.node_selector
    if component.node_name:
        node_selector = {"kubernetes.io/hostname": component.node_name}
    context = {}
    if chart == "extended-probes":
        probe_name = probe_kind(component.release_name or "")
        context = _extended_chart_context(model, targets, probe_name, probe_states)
    elif chart == "ocudu-metrics":
        context["ocudu_sources"] = ocudu_sources(model)
    elif chart == "kube-prometheus-stack":
        context["target_namespaces"] = sorted({model.monitoring.namespace} | {t["namespace"] for t in targets})
    rendered = VALUES_TEMPLATES.get_template(f"{chart}.yaml.j2").render(
        component=component,
        monitoring=model.monitoring,
        model=model,
        targets=targets,
        node_selector=node_selector,
        **context,
    )
    values = deep_merge(yaml.safe_load(defaults.read_text()) or {}, yaml.safe_load(rendered))
    if chart == "monitoring-controller":
        values = deep_merge(values, controller_values(model.monitoring, targets, probe_states))
    return deep_merge(values, component.advanced_config)


def _enabled_components(model: MonitoringModel) -> set[str]:
    monitoring = model.monitoring
    core = str(model.core.implementation)
    ran = str(model.ran.implementation)
    if monitoring.core_kpis and core != "open5gs":
        raise ValueError("Core KPI calculator requires Open5GS")
    if monitoring.ran_kpis and ran not in {"oai", "ocudu"}:
        raise ValueError("RAN KPIs support OAI and OCUDU")
    enabled = {"integration", "prometheus", "grafana"}
    if monitoring.needs_session_stack:
        enabled.update(("redis", "ue-mapper-api", "monitoring-controller"))
    if monitoring.loki:
        enabled.update(("loki", "promtail"))
    if monitoring.core_kpis:
        enabled.add("kpi-calculator-open5gs")
    if monitoring.ran_kpis:
        enabled.add("ocudu-metrics" if ran == "ocudu" else "kpi-calculator-oai")
    return enabled


def resolve_components(model: MonitoringModel, targets: list[dict]) -> dict[str, DeployedMonitoringComponent]:
    """Build enabled monitoring components and their Helm values in installation order."""
    monitoring = model.monitoring
    catalog = yaml.safe_load((ASSETS / "components.yaml").read_text())
    enabled = _enabled_components(model)
    definitions = {name: data for name, data in catalog["components"].items() if name in enabled}
    probe_states = _probe_states(model)
    kinds = probe_states if monitoring.probe_controller_image else enabled_probes(monitoring.extended)
    for kind in kinds:
        definitions[release_name(kind)] = {
            "chart_ref": "extended-probes",
            "pod_selector": [f"app={workload(kind)[1]}"],
        }
    components = {}
    for name, definition in definitions.items():
        data = (
            {"release_name": name, "chart_ref": name, "pod_selector": ["app=" + name]}
            | catalog["defaults"]
            | definition
        )
        image = component_image_defaults(name, monitoring.probe_controller_image)
        if image:
            data["image"] = image.copy()
        if name in monitoring.components:
            data = deep_merge(
                data, monitoring.components[name].model_dump(mode="json", exclude_unset=True, exclude_none=True)
            )
        component = DeployedMonitoringComponent.model_validate(data)
        if (
            name == "ue-mapper-api"
            and monitoring.observability.mapper_coverage
            and (image := monitoring.observability.mapper_image)
        ):
            repository, tag = split_image(image)
            if not repository or not tag or "@" in image:
                raise ValueError("mapper_image must use an explicit repository:tag")
            component.image.repository = repository
            component.image.image_tag = tag
        component.namespace = monitoring.namespace
        component.chart_values = _render_chart_values(component, model, targets, probe_states)
        if probe_kind(name) and component.chart_values.get("settings", {}).get("paused"):
            component.pod_selector = []
        if not component.chart_version:
            component.chart_ref = f"{REMOTE_CHARTS}/{Path(component.chart_ref).name}"
        components[name] = component
    return components


async def run_monitoring_component_action(
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
        return result[output_key]
    return None
