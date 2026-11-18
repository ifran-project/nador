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
    DeployedMonitoringComponent,
    MonitoringDeployment,
    MonitoringModel,
    ResolvedTarget,
    component_image_defaults,
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


def _probe_states(model: MonitoringModel) -> dict[str, bool]:
    """Running (True) or paused (False) state of each probe release the controller manages."""
    monitoring = model.monitoring
    if not monitoring.uses_controller:
        return {}
    states = controlled_probes(monitoring.host_probe_config(), list(monitoring.host_probe_images()))
    kinds = {str(nf.type) for nf in model.ran.components.values()}
    if not (("du" in kinds and kinds.intersection({"cu", "cu_cp", "cu_up"})) or {"cu_cp", "cu_up"} <= kinds):
        if states.get(SPLIT_SIGNALING):
            raise ValueError("split_signaling requires a modeled F1 or E1 split")
        states.pop(SPLIT_SIGNALING, None)
    return states


def resolve_targets(model: MonitoringModel) -> list[ResolvedTarget]:
    """Resolve stable selectors from the same release naming used by HelmManagement."""
    result = []
    for target in model.monitoring.targets:
        components = model.ues if target.category == "ue" else getattr(model, target.category).components
        if target.component not in components:
            raise ValueError(f"Unknown {target.category} component {target.component!r}")
        component = components[target.component]
        namespace = getattr(component, "namespace", None)
        implementation = getattr(component, "implementation", None)
        if not namespace or implementation is None:
            raise ValueError(f"{target.component}: namespace is unresolved; pass the latest deployed manager")
        if target.role:
            expected = {"amf": {"amf"}, "smf": {"smf"}, "upf": {"upf"}, "gnb": {"gnb", "cu", "cu_up"}}
            if component.type not in expected[target.role]:
                raise ValueError(f"{target.component}: {component.type} cannot be a {target.role} packet target")
        selector = target.selector or {"app": build_k8s_safe(f"{implementation}-{target.component}")}
        result.append(
            ResolvedTarget(
                **target.model_dump(exclude={"selector"}),
                selector=selector,
                namespace=namespace,
                implementation=str(implementation),
                label_selectors=[f"{key}={value}" for key, value in selector.items()],
            )
        )
    return result


def _host_probe_chart_context(
    model: MonitoringModel, targets: list[ResolvedTarget], probe_name: str, probe_states: dict[str, bool]
) -> dict:
    monitoring = model.monitoring
    extended_targets = [
        target.values() | {"kind": str(getattr(model, target.category).components[target.component].type)}
        for target in targets
        if target.category in ("ran", "core")
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
    config = monitoring.host_probe_config()
    selected = config["probe_targets"].get(probe_name)
    if selected:
        available = {f"{t['category']}/{t['component']}" for t in extended_targets}
        if set(selected) - available:
            raise ValueError(f"Unresolved probe targets for {probe_name}: {sorted(set(selected) - available)}")
        extended_targets = [t for t in extended_targets if f"{t['category']}/{t['component']}" in selected]
    extended_config = probe_values(config, probe_name)
    if probe_name in probe_states:
        extended_config["paused"] = not probe_states[probe_name]
        if not probe_states[probe_name]:
            for flag in set(PROBES[probe_name]) - NOT_ENABLED_WHILE_PAUSED:
                extended_config[flag] = True
    return {
        "extended_targets": extended_targets,
        "extended_config": extended_config,
        "probe_image": monitoring.host_probe_images().get(probe_name),
    }


def _controller_values(
    monitoring: MonitoringDeployment, targets: list[ResolvedTarget], probe_states: dict[str, bool]
) -> dict:
    """Build controller values.

    Single-anchor deployments describe their gNB/UPF through probeControlTargets (targets.json);
    multi-anchor deployments pass explicit per-request target lists instead.
    """
    metrics = monitoring.metrics
    anchors = [target.values() for target in targets if target.role in ("gnb", "upf")]
    upfs = [target for target in anchors if target["role"] == "upf"]
    by_role = {target["role"]: target for target in anchors}
    controller_image = monitoring.image("controller") if monitoring.uses_controller else None
    has_session_context = {"amf", "smf"} <= {target.role for target in targets}
    scoped_probes_allowed = controller_image is not None and has_session_context

    upf_transit = metrics.upf_transit
    one_way: dict = {
        "image": monitoring.image("one_way_probe") if monitoring.one_way_probes else None,
        "sample": monitoring.tuning.one_way_sample,
        "test_port": upf_transit.test_port if upf_transit else 0,
        "n3": {
            "enabled": metrics.running("n3_one_way"),
            "log_samples": bool(metrics.n3_one_way and metrics.n3_one_way.log_samples),
        },
        "upf_transit": {
            "enabled": metrics.running("upf_transit"),
            "peer": str(upf_transit.peer) if upf_transit else None,
        },
    }
    if monitoring.one_way_probes and monitoring.separate_one_way_targets:
        one_way["one_way_targets"] = anchors

    rtt = metrics.rtt
    rtt_request: dict = {
        "enabled": metrics.running("rtt"),
        "allowed": scoped_probes_allowed and bool(anchors),
        "image": monitoring.image("rtt_probe"),
        "imsis": rtt.imsis if rtt else [],
        "slices": rtt.slices if rtt else [],
    }
    if rtt and rtt.packet_pairing:
        rtt_request["pairer_image"] = monitoring.image("packet_pairer")
    throughput_image = monitoring.image("throughput_probe") if metrics.throughput else None
    throughput = {
        "enabled": metrics.running("throughput"),
        "allowed": scoped_probes_allowed and bool(upfs) and throughput_image is not None,
        "image": throughput_image,
    }
    paths_metric = metrics.user_plane_paths
    path_image = monitoring.image("path_probe") if paths_metric else None
    paths = {
        "enabled": metrics.running("user_plane_paths"),
        "allowed": bool(anchors) and path_image is not None,
        "image": path_image,
        "port": paths_metric.port if paths_metric else 9403,
        "interval": paths_metric.interval_seconds if paths_metric else 5,
    }
    if monitoring.multi_anchor and controller_image:
        rtt_request["targets"] = paths["targets"] = anchors
        throughput["targets"] = upfs

    return {
        "targetNamespaces": sorted({target.namespace for target in targets}),
        "operationTimeoutSeconds": monitoring.operation_timeout_seconds,
        "probeControllerImage": controller_image,
        "cleanupImage": monitoring.image("controller"),
        "probeControlTargets": {} if monitoring.multi_anchor else by_role,
        "extendedProbeStates": probe_states,
        # Workloads the controller may pause and resume; RBAC is generated from this list.
        "extendedProbeWorkloads": [
            dict(zip(("resource", "name"), workload(kind), strict=True)) for kind in sorted(probe_states)
        ],
        "probeApiTlsSecret": monitoring.probe_api_tls_secret,
        "probeApiTokenSecret": monitoring.probe_api_token_secret,
        "probeControl": one_way,
        "rttRequest": rtt_request,
        "throughputRequest": throughput,
        "pathRequest": paths,
    }


def _chart_name(component: DeployedMonitoringComponent) -> str:
    """Chart directory or repository chart name, without its repository or path prefix."""
    if not component.chart_ref:
        raise ValueError(f"{component.release_name}: chart_ref is not set")
    return component.chart_ref.rsplit("/", 1)[-1]


def _render_chart_values(
    component: DeployedMonitoringComponent,
    model: MonitoringModel,
    targets: list[ResolvedTarget],
    probe_states: dict[str, bool],
) -> dict:
    """Render chart configuration before applying explicit user overrides."""
    chart = _chart_name(component)
    defaults = ASSETS / "defaults" / f"{chart}.yaml" if component.chart_version else CHARTS / chart / "values.yaml"
    node_selector = component.node_selector
    if component.node_name:
        node_selector = {"kubernetes.io/hostname": component.node_name}
    context = {}
    if chart == "extended-probes":
        probe_name = probe_kind(component.release_name or "")
        context = _host_probe_chart_context(model, targets, probe_name, probe_states)
    elif chart == "ocudu-metrics":
        context["ocudu_sources"] = ocudu_sources(model)
    elif chart == "kube-prometheus-stack":
        context["target_namespaces"] = sorted({model.monitoring.namespace} | {t.namespace for t in targets})
    rendered = VALUES_TEMPLATES.get_template(f"{chart}.yaml.j2").render(
        component=component,
        monitoring=model.monitoring,
        model=model,
        targets=[target.values() for target in targets],
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
    metrics = monitoring.metrics
    if metrics.core_kpis and core != "open5gs":
        raise ValueError("Core KPI calculator requires Open5GS")
    if metrics.ran_kpis and ran not in {"oai", "ocudu"}:
        raise ValueError("RAN KPIs support OAI and OCUDU")
    enabled = {"integration", "prometheus", "grafana"}
    if monitoring.needs_session_stack:
        enabled.update(("redis", "ue-mapper-api", "monitoring-controller"))
    if metrics.logs:
        enabled.update(("loki", "promtail"))
    if metrics.core_kpis:
        enabled.add("kpi-calculator-open5gs")
    if metrics.ran_kpis:
        enabled.add("ocudu-metrics" if ran == "ocudu" else "kpi-calculator-oai")
    return enabled


def resolve_components(model: MonitoringModel, targets: list[ResolvedTarget]) -> dict[str, DeployedMonitoringComponent]:
    """Build enabled monitoring components and their Helm values in installation order."""
    monitoring = model.monitoring
    catalog = yaml.safe_load((ASSETS / "components.yaml").read_text())
    enabled = _enabled_components(model)
    definitions = {name: data for name, data in catalog["components"].items() if name in enabled}
    probe_states = _probe_states(model)
    kinds = probe_states if monitoring.uses_controller else enabled_probes(monitoring.host_probe_config())
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
        image = component_image_defaults(name, monitoring.images)
        if image:
            data["image"] = image.copy()
        if name in monitoring.components:
            data = deep_merge(
                data, monitoring.components[name].model_dump(mode="json", exclude_unset=True, exclude_none=True)
            )
        component = DeployedMonitoringComponent.model_validate(data)
        component.namespace = monitoring.namespace
        component.chart_values = _render_chart_values(component, model, targets, probe_states)
        if probe_kind(name) and component.chart_values.get("settings", {}).get("paused"):
            component.pod_selector = []
        if not component.chart_version:
            component.chart_ref = f"{REMOTE_CHARTS}/{_chart_name(component)}"
        components[name] = component
    return components
