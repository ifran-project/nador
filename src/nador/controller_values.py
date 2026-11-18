"""Build the Helm values for the monitoring controller."""

from nador.models import DEFAULT_IMAGES, MonitoringDeployment
from nador.probe_catalog import workload


def controller_values(monitoring: MonitoringDeployment, targets: list[dict], probe_states: dict) -> dict:
    """Keep the legacy single-role target map alongside the explicit multi-target requests."""
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
