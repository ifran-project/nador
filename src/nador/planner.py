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
from metamodel_3gpp.models.metamodel import MetaModel

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
REMOTE_CHARTS = "/var/lib/nador/charts"  # root-owned on the control node, unlike a shared /tmp
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
    probe_targets = [
        target.values() | {"kind": str(getattr(model, target.category).components[target.component].type)}
        for target in targets
        if target.category in ("ran", "core")
    ]
    existing = {(target["category"], target["component"]) for target in probe_targets}
    for category in ("ran", "core"):
        for name, nf in getattr(model, category).components.items():
            if (category, name) not in existing and nf.namespace:
                probe_targets.append(
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
        available = {f"{t['category']}/{t['component']}" for t in probe_targets}
        if set(selected) - available:
            raise ValueError(f"Unresolved probe targets for {probe_name}: {sorted(set(selected) - available)}")
        probe_targets = [t for t in probe_targets if f"{t['category']}/{t['component']}" in selected]
    probe_config = probe_values(config, probe_name)
    if probe_name in probe_states:
        probe_config["paused"] = not probe_states[probe_name]
        if not probe_states[probe_name]:
            for flag in set(PROBES[probe_name]) - NOT_ENABLED_WHILE_PAUSED:
                probe_config[flag] = True
    return {
        "probe_targets": probe_targets,
        "probe_config": probe_config,
        "probe_image": monitoring.host_probe_images().get(probe_name),
    }


def _controller_values(
    monitoring: MonitoringDeployment, targets: list[ResolvedTarget], probe_states: dict[str, bool]
) -> dict:
    """Build controller values.

    Single-anchor deployments describe their gNB/UPF through ``anchors`` (targets.json);
    multi-anchor deployments pass explicit per-request target lists instead.
    """
    anchors = [target.values() for target in targets if target.role in ("gnb", "upf")]
    controller_image = monitoring.image("controller") if monitoring.uses_controller else None
    # Session-scoped probes need the session context, which the AMF and SMF sniffers provide.
    scoped = controller_image is not None and {"amf", "smf"} <= {target.role for target in targets}
    # Explicit target lists: always with several anchors; for RTT also when rtt.at leaves some out.
    explicit = monitoring.multi_anchor and controller_image is not None
    return {
        "targetNamespaces": sorted({target.namespace for target in targets}),
        "operationTimeoutSeconds": monitoring.operation_timeout_seconds,
        "controllerImage": controller_image,
        "cleanupImage": monitoring.image("controller"),
        "anchors": {} if monitoring.multi_anchor else {target["role"]: target for target in anchors},
        "hostProbeStates": probe_states,
        # Workloads the controller may pause and resume; RBAC is generated from this list.
        "hostProbeWorkloads": [
            dict(zip(("resource", "name"), workload(kind), strict=True)) for kind in sorted(probe_states)
        ],
        "probeApiTlsSecret": monitoring.probe_api_tls_secret,
        "probeApiTokenSecret": monitoring.probe_api_token_secret,
        "oneWay": _one_way_values(monitoring, anchors),
        "rtt": _rtt_values(monitoring, anchors, scoped=scoped, controller=controller_image is not None),
        "throughput": _throughput_values(monitoring, anchors, scoped=scoped, explicit=explicit),
        "userPlanePaths": _paths_values(monitoring, anchors, explicit=explicit),
    }


def _one_way_values(monitoring: MonitoringDeployment, anchors: list[dict]) -> dict:
    """N3 one-way delay and UPF transit time: one probe image, two metrics."""
    metrics = monitoring.metrics
    upf_transit = metrics.upf_transit
    one_way: dict = {
        "image": monitoring.image("one_way_probe") if monitoring.one_way_probes else None,
        "sample": monitoring.tuning.one_way_sample,
        "test_port": upf_transit.test_port if upf_transit else 0,
        "n3_one_way": {
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
    return one_way


def _rtt_values(monitoring: MonitoringDeployment, anchors: list[dict], *, scoped: bool, controller: bool) -> dict:
    """RTT probes at the anchors rtt.at names, for the UEs and slices it selects."""
    rtt = monitoring.metrics.rtt
    at = [target for target in anchors if rtt is None or target["role"] in rtt.at]
    values: dict = {
        "enabled": monitoring.metrics.running("rtt"),
        "allowed": scoped and bool(at),
        "image": monitoring.image("rtt_probe"),
        "imsis": rtt.imsis if rtt else [],
        "slices": rtt.slices if rtt else [],
    }
    if rtt and rtt.packet_pairing:
        values["pairer_image"] = monitoring.image("packet_pairer")
    if controller and (monitoring.multi_anchor or len(at) < len(anchors)):
        values["targets"] = at
    return values


def _throughput_values(monitoring: MonitoringDeployment, anchors: list[dict], *, scoped: bool, explicit: bool) -> dict:
    """Throughput probes on the UPFs."""
    upfs = [target for target in anchors if target["role"] == "upf"]
    image = monitoring.image("throughput_probe") if monitoring.metrics.throughput else None
    values: dict = {
        "enabled": monitoring.metrics.running("throughput"),
        "allowed": scoped and bool(upfs) and image is not None,
        "image": image,
    }
    if explicit:
        values["targets"] = upfs
    return values


def _paths_values(monitoring: MonitoringDeployment, anchors: list[dict], *, explicit: bool) -> dict:
    """User-plane path probes on the gNBs and UPFs."""
    paths = monitoring.metrics.user_plane_paths
    image = monitoring.image("path_probe") if paths else None
    values: dict = {
        "enabled": monitoring.metrics.running("user_plane_paths"),
        "allowed": bool(anchors) and image is not None,
        "image": image,
        "port": paths.port if paths else 9403,
        "interval": paths.interval_seconds if paths else 5,
    }
    if explicit:
        values["targets"] = anchors
    return values


def _chart_name(component: DeployedMonitoringComponent) -> str:
    """Chart directory or repository chart name, without its repository or path prefix."""
    if not component.chart_ref:
        raise ValueError(f"{component.release_name}: chart_ref is not set")
    return component.chart_ref.rsplit("/", 1)[-1]


def _log_namespaces(model: MonitoringModel) -> list[str]:
    """Namespaces whose pods' logs are kept: the RAN's, the core's and/or NADOR's own."""
    groups = model.monitoring.metrics.log_groups
    namespaces = {model.monitoring.namespace} if "host" in groups else set()
    for group in ("ran", "core"):
        if group in groups:
            components = getattr(model, group).components.values()
            namespaces |= {component.namespace for component in components if component.namespace}
    return sorted(namespaces)


def _render_chart_values(
    component: DeployedMonitoringComponent,
    model: MonitoringModel,
    targets: list[ResolvedTarget],
    probe_states: dict[str, bool],
    *,
    ue_names: dict[str, str],
    ocudu_dus: list[str],
) -> dict:
    """Render chart configuration before applying explicit user overrides."""
    chart = _chart_name(component)
    defaults = ASSETS / "defaults" / f"{chart}.yaml" if component.chart_version else CHARTS / chart / "values.yaml"
    node_selector = component.node_selector
    if component.node_name:
        node_selector = {"kubernetes.io/hostname": component.node_name}
    context = {}
    if chart == "host-probes":
        probe_name = probe_kind(component.release_name or "")
        context = _host_probe_chart_context(model, targets, probe_name, probe_states)
    elif chart == "ocudu-metrics":
        context["ocudu_sources"] = ocudu_sources(model)
    elif chart == "rnti-mapper":
        context |= {
            "ue_names": ue_names,
            "ocudu_dus": ocudu_dus,
            "components": _enabled_components(model),
            "build_k8s_safe": build_k8s_safe,
        }
    elif chart == "promtail":
        context["log_namespaces"] = _log_namespaces(model)
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
    ran = str(model.ran.implementation)
    metrics = monitoring.metrics
    if metrics.ran_kpis and ran not in {"oai", "ocudu"}:
        raise ValueError("RAN KPIs support OAI and OCUDU")
    enabled = {"integration", "prometheus", "grafana"}
    if monitoring.needs_session_stack:
        enabled.update(("redis", "ue-mapper-api", "monitoring-controller"))
    if metrics.log_groups:
        enabled.update(("loki", "promtail"))
    if metrics.ran_kpis:
        enabled.add("ocudu-metrics" if ran == "ocudu" else "kpi-calculator-oai")
    if (metrics.ran_kpis or metrics.csi is not None) and _has_rrc(model):
        # RNTI -> UE for the dashboard panels per RNTI (RAN KPIs, CSI)
        enabled.add("rnti-mapper")
    return enabled


def ue_names(metamodel: MetaModel | None) -> dict[str, str]:
    """IMSI -> UE name, from the UEs of the meta model (their SUPI is the IMSI)."""
    if metamodel is None:
        return {}
    return {ue.config.supi: name for name, ue in metamodel.ues.items() if ue.config is not None}


def ocudu_dus(metamodel: MetaModel | None, model: MonitoringModel) -> list[str]:
    """``"<CU-CP> <gNB-DU ID> <DU>"`` of each OCUDU DU, for the rnti-mapper to name the DU of each UE.

    The gNB-DU ID is the one the CU-CP logs in its F1AP lines (``du=``): ``advanced_config.gnb_du_id`` when it is set,
    else ``gnbDuId`` of the meta model. The DUs of a CU-CP that would share an ID are left out: their UEs stay on the
    CU-CP rather than on the wrong DU.
    """
    meta = metamodel.ran.components if metamodel is not None and metamodel.ran is not None else {}
    ids: dict[str, dict[int, list[str]]] = {}
    for name, component in model.ran.components.items():
        if str(component.implementation) != "ocudu" or str(component.type) != "du" or name not in meta:
            continue
        config = meta[name].config
        cu_cp = getattr(config, "cu_cp", None)
        advanced = component.advanced_config
        if advanced is not None and "gnb_du_id" in advanced.model_fields_set:
            du_id = advanced.gnb_du_id
        else:
            du_id = getattr(config, "gnbDuId", None)
        if cu_cp and du_id is not None:
            ids.setdefault(cu_cp, {}).setdefault(int(du_id), []).append(name)
    return [
        f"{cu_cp} {du_id} {dus[0]}"
        for cu_cp, by_id in sorted(ids.items())
        for du_id, dus in sorted(by_id.items())
        if len(dus) == 1
    ]


def _has_rrc(model: MonitoringModel) -> bool:
    """Whether an OAI or OCUDU gNB, CU or CU-CP runs the RRC: the rnti-mapper reads its UE contexts."""
    return any(
        str(ran.type) in ("gnb", "cu", "cu_cp") and str(ran.implementation) in ("oai", "ocudu")
        for ran in model.ran.components.values()
    )


def resolve_components(
    model: MonitoringModel,
    targets: list[ResolvedTarget],
    ue_names: dict[str, str] | None = None,
    ocudu_dus: list[str] | None = None,
) -> dict[str, DeployedMonitoringComponent]:
    """Build enabled monitoring components and their Helm values in installation order.

    ``ue_names`` maps an IMSI to the UE's name and ``ocudu_dus`` names the DUs of the OCUDU CU-CPs
    (from ``ocudu_dus()``), for the rnti-mapper.
    """
    monitoring = model.monitoring
    catalog = yaml.safe_load((ASSETS / "components.yaml").read_text())
    enabled = _enabled_components(model)
    definitions = {name: data for name, data in catalog["components"].items() if name in enabled}
    probe_states = _probe_states(model)
    kinds = probe_states if monitoring.uses_controller else enabled_probes(monitoring.host_probe_config())
    for kind in kinds:
        definitions[release_name(kind)] = {
            "chart_ref": "host-probes",
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
        component.chart_values = _render_chart_values(
            component, model, targets, probe_states, ue_names=ue_names or {}, ocudu_dus=ocudu_dus or []
        )
        if probe_kind(name) and component.chart_values.get("settings", {}).get("paused"):
            component.pod_selector = []
        if not component.chart_version:
            component.chart_ref = f"{REMOTE_CHARTS}/{_chart_name(component)}"
        components[name] = component
    return components
