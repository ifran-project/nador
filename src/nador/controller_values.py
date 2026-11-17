"""Build the Helm values for the monitoring controller."""

from .models import MonitoringDeployment


def controller_values(monitoring: MonitoringDeployment, targets: list[dict], probe_states: dict) -> dict:
    """Keep the legacy single-role target map alongside the explicit multi-target requests."""
    anchors = [target for target in targets if target["role"] in ("gnb", "upf")]
    upfs = [target for target in anchors if target["role"] == "upf"]
    by_role = {target["role"]: target for target in anchors}
    controller_image = monitoring.probe_controller_image
    has_session_context = {"amf", "smf"} <= {target["role"] for target in targets}
    scoped_probes_allowed = controller_image is not None and has_session_context
    observation = monitoring.observability

    legacy = {
        "watchNamespaces": sorted({target["namespace"] for target in anchors}),
        "probeMode": next(iter(by_role)) if len(by_role) == 1 else "both",
    }
    for role, target in by_role.items():
        legacy[f"{role}Interface"] = target["interface"]
        legacy[f"{role}LatencyMode"] = target["latency_mode"]

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
        "controller": legacy,
        "images": {"probe": monitoring.images.probe},
        "throughput": {"enabled": monitoring.throughput, "probeImage": monitoring.images.throughput},
        "latency": {"enabled": monitoring.latency},
        "targets": targets,
        "operationTimeoutSeconds": monitoring.operation_timeout_seconds,
        "probeControllerImage": controller_image,
        "probeControlTargets": {} if monitoring.multi_anchor else by_role,
        "extendedProbeStates": probe_states,
        "probeApiTlsSecret": monitoring.probe_api_tls_secret,
        "probeApiTokenSecret": monitoring.probe_api_token_secret,
        "probeControl": one_way,
        "rttRequest": rtt,
        "throughputRequest": throughput,
        "pathRequest": paths,
    }
