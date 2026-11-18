"""Turn a monitoring profile and the deployed network into Helm releases and their values.

Nothing here contacts a cluster: ``resolve_targets`` finds each monitored network
function, and ``resolve_components`` decides which charts to install and renders
their values.
"""

from pathlib import Path

import yaml
from ifran.utils.k8s import build_k8s_safe
from ifran.utils.utils import deep_merge
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from nador.config import (
    DEFAULT_IMAGES,
    DeployedMonitoringComponent,
    MonitoringDeployment,
    MonitoringModel,
    component_image_defaults,
    split_image,
)
from nador.probes import (
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
from nador.ran_kpis import ocudu_sources

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


def _controller_values(monitoring: MonitoringDeployment, targets: list[dict], probe_states: dict) -> dict:
    """Build controller values.

    Single-anchor deployments describe their gNB/UPF through probeControlTargets (targets.json);
    multi-anchor deployments pass explicit per-request target lists instead.
    """
    anchors = [target for target in targets if target["role"] in ("gnb", "upf")]
    upfs = [target for target in anchors if target["role"] == "upf"]
    by_role = {target["role"]: target for target in anchors}
    controller_image = monitoring.probe_controller_image
    has_session_context = {"amf", "smf"} <= {target["role"] for target in targets}
    scoped_probes_allowed = controller_image is not None and has_session_context
    observation = monitoring.observability

    one_way = monitoring.one_way.model_dump(mode="json")
    if monitoring.one_way.image and monitoring.separate_one_way_targets:
        one_way["one_way_targets"] = anchors

    rtt = {
        "enabled": monitoring.latency,
        "allowed": scoped_probes_allowed and bool(anchors),
        "image": monitoring.images.probe,
        "imsis": monitoring.rtt_imsis,
        "slices": monitoring.rtt_slices,
    }
    if monitoring.packet_pairing:
        pairer = DEFAULT_IMAGES["components"]["packet-pairer"]
        rtt["pairer_image"] = f"{pairer['repository']}:{pairer['image_tag']}"
    throughput = {
        "enabled": monitoring.throughput,
        "allowed": scoped_probes_allowed and bool(upfs) and monitoring.external_throughput_image is not None,
        "image": monitoring.external_throughput_image,
    }
    paths = {
        "enabled": observation.user_plane_paths,
        "allowed": bool(anchors) and monitoring.external_path_image is not None,
        "image": monitoring.external_path_image,
        "port": observation.path_port,
        "interval": observation.path_interval_seconds,
    }
    if monitoring.multi_anchor and controller_image:
        rtt["targets"] = paths["targets"] = anchors
        throughput["targets"] = upfs

    return {
        "targetNamespaces": sorted({target["namespace"] for target in targets}),
        "operationTimeoutSeconds": monitoring.operation_timeout_seconds,
        "probeControllerImage": controller_image,
        "cleanupImage": controller_image or DEFAULT_IMAGES["external_probes"]["controller"],
        "probeControlTargets": {} if monitoring.multi_anchor else by_role,
        "extendedProbeStates": probe_states,
        # Workloads the controller may pause and resume; RBAC is generated from this list.
        "extendedProbeWorkloads": [
            dict(zip(("resource", "name"), workload(kind), strict=True)) for kind in sorted(probe_states)
        ],
        "probeApiTlsSecret": monitoring.probe_api_tls_secret,
        "probeApiTokenSecret": monitoring.probe_api_token_secret,
        "probeControl": one_way,
        "rttRequest": rtt,
        "throughputRequest": throughput,
        "pathRequest": paths,
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
        values = deep_merge(values, _controller_values(model.monitoring, targets, probe_states))
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
