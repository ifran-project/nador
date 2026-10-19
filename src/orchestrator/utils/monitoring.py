"""Resolve model components and prepare monitoring Helm values."""

from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from orchestrator.models.deployment import DeploymentModel
from orchestrator.models.monitoring import DeployedMonitoringComponent
from orchestrator.utils.k8s import build_k8s_safe
from orchestrator.utils.utils import deep_merge

ASSETS = Path(__file__).resolve().parents[1] / "monitoring_assets"
CHARTS = ASSETS / "charts"
VALUES_TEMPLATES = Environment(
    loader=FileSystemLoader(ASSETS / "templates"),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


# Resolve components into namespaces, pod selectors and capture settings.
def resolve_targets(model: DeploymentModel) -> list[dict]:
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
    model: DeploymentModel,
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
def resolve_components(model: DeploymentModel, targets: list[dict]) -> dict[str, DeployedMonitoringComponent]:
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
        components[name].chart_values = _component_values(components[name], model, targets, components)
    return {name: component for name, component in components.items() if name in enabled}
