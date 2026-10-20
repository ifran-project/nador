"""Resolve model components and prepare monitoring Helm values."""

from ipaddress import ip_address
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from orchestrator.models.ansible import AnsibleParams
from orchestrator.models.k8s import MultusNetworkStatus
from orchestrator.models.provisioning import ProvisionableNode
from orchestrator.utils.ansible import get_ansible_inventory_line, run_ansible_role
from orchestrator.utils.k8s import build_k8s_safe
from orchestrator.utils.utils import InvalidResourceStateError, deep_merge

from orchestrator_monitoring.models import DeployedMonitoringComponent, MonitoringModel

ASSETS = Path(__file__).resolve().parent / "assets"
CHARTS = ASSETS / "charts"
REMOTE_CHARTS = "/tmp/monitoring-charts"
VALUES_TEMPLATES = Environment(
    loader=FileSystemLoader(ASSETS / "templates"),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


# Resolve components into namespaces, pod selectors and capture settings.
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
            raise ValueError(f"{target.component}: namespace is unresolved; pass the la<YOUR_PASSWORD> deployed manager")
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


# Render this chart's dynamic values, then apply explicit user overrides.
def _component_values(
    component: DeployedMonitoringComponent,
    model: MonitoringModel,
    targets: list[dict],
    components: dict[str, DeployedMonitoringComponent],
) -> dict:
    chart = component.chart_ref.rsplit("/", 1)[-1]
    defaults = ASSETS / "defaults" / f"{chart}.yaml" if component.chart_version else CHARTS / chart / "values.yaml"
    node_selector = component.node_selector
    if component.node_name:
        node_selector = {"kubernetes.io/hostname": component.node_name}
    packet_targets = {target["role"]: target for target in targets if target["role"] in ("gnb", "upf")}
    template = VALUES_TEMPLATES.get_template(f"{chart}.yaml.j2")
    rendered = template.render(
        component=component,
        components=components,
        monitoring=model.monitoring,
        model=model,
        targets=targets,
        node_selector=node_selector,
        target_namespaces=sorted({model.monitoring.namespace} | {target["namespace"] for target in targets}),
        packet_targets=packet_targets,
        packet_namespaces=sorted({target["namespace"] for target in targets if target["role"] in ("gnb", "upf")}),
    )
    values = deep_merge(yaml.safe_load(defaults.read_text()) or {}, yaml.safe_load(rendered))
    return deep_merge(values, component.advanced_config)


# Resolve enabled component models once, then build values directly for each chart.
def resolve_components(model: MonitoringModel, targets: list[dict]) -> dict[str, DeployedMonitoringComponent]:
    monitoring = model.monitoring
    defaults = yaml.safe_load((ASSETS / "components.yaml").read_text())
    if monitoring.core_kpis and str(model.core.implementation) != "open5gs":
        raise ValueError("Core KPI calculator requires Open5GS")
    if monitoring.ran_kpis and str(model.ran.implementation) != "oai":
        raise ValueError("RAN KPI calculator requires OAI")
    enabled = {"prometheus", "grafana"}
    if monitoring.loki:
        enabled.update(("loki", "promtail"))
    if monitoring.latency:
        enabled.update(("redis", "ue-mapper-api", "monitoring-controller"))
        if monitoring.packet_pairing:
            enabled.add("packet-pairer")
    if monitoring.core_kpis:
        enabled.add("kpi-calculator-open5gs")
    if monitoring.ran_kpis:
        enabled.add("kpi-calculator-oai")
    if monitoring.state.installed_namespace and set(monitoring.components) != enabled:
        raise ValueError("Remove monitoring before changing enabled components")
    components = {}
    for name, definition in defaults["components"].items():
        data = (
            {"type": name, "release_name": name, "chart_ref": name, "pod_selector": ["app=" + name]}
            | defaults["defaults"]
            | definition
        )
        if name in monitoring.components:
            data = deep_merge(
                data, monitoring.components[name].model_dump(mode="json", exclude_unset=True, exclude_none=True)
            )
        component = DeployedMonitoringComponent.model_validate(data)
        component.type = name
        component.pod_selector = component.pod_selector or definition.get("pod_selector", ["app=" + name])
        component.namespace = monitoring.state.installed_namespace or monitoring.namespace
        components[name] = component
    for name in enabled:
        component = components[name]
        component.chart_values = _component_values(component, model, targets, components)
        component.build_dependencies = not bool(component.chart_version)
        if component.build_dependencies:
            component.chart_ref = f"{REMOTE_CHARTS}/{Path(component.chart_ref).name}"
    return {name: component for name, component in components.items() if name in enabled}


# Execute monitoring's chart role without changing the orchestrator's 5G executor.
async def run_monitoring_component_action(
    *,
    component_name: str,
    component_deployment: DeployedMonitoringComponent,
    action: str,
    node_name: str,
    node_data: ProvisionableNode,
    ansible_params: AnsibleParams,
    command_to_run: list[str] | None = None,
    force: bool = False,
    quiet: bool = False,
    log_container: str | None = None,
    since_seconds: int | None = None,
    kubeconfig: str | None = None,
    timeout_seconds: int = 300,
) -> str | None:
    """Run one monitoring component action and record successful installation/removal."""
    component = component_deployment
    if not force:
        if action == "install" and component.is_installed:
            raise InvalidResourceStateError(f"Monitoring component {component_name!r} is already installed")
        if action in ("uninstall", "get_logs", "run_cmd") and not component.is_installed:
            raise InvalidResourceStateError(f"Monitoring component {component_name!r} is not installed")
    if action == "run_cmd" and not command_to_run:
        raise ValueError("A command is required for run_cmd")

    chart_name = component.release_name or component_name
    payload = component.model_dump(mode="json")
    payload["pod_selector"] = component.pod_selector or [f"app.kubernetes.io/instance={chart_name}"]
    output_key = {
        "install": "network_info",
        "get_logs": "logs",
        "run_cmd": "command_output",
    }.get(action)
    try:
        result = await run_ansible_role(
            role="monitoring/component",
            hosts=node_name,
            inventory=get_ansible_inventory_line(node_name, node_data),
            become=True,
            inputs={
                "helm_action": action,
                "component": payload,
                "chart_name": chart_name,
                "output_with_multus": component.multus.enabled,
                "kubeconfig": kubeconfig,
                "timeout_seconds": timeout_seconds,
                "since_seconds": since_seconds,
                "log_container": log_container,
                "command": command_to_run,
            },
            output_keys=[output_key] if output_key else None,
            runner_path=f"{ansible_params.runner_path}/monitoring/{component_name}/{action}",
            roles_paths=[str(ASSETS / "roles"), *ansible_params.roles_paths],
            quiet=quiet,
        )
    except Exception as error:
        raise RuntimeError(f"Monitoring component {component_name!r} Helm {action} not successful") from error

    if action == "install":
        component.network_info = (
            [MultusNetworkStatus.model_validate(item) for item in result["network_info"]]
            if component.multus.enabled
            else ip_address(result["network_info"])
        )
        component.is_installed = True
    elif action == "uninstall":
        component.is_installed = False
        component.network_info = None
    elif output_key:
        return result[output_key]
    return None
