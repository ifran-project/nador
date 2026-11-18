"""Extended collectors: their feature flags, Helm releases and Kubernetes workloads.

Every probe name and workload name is derived here, so the Python planner and the
controller's RBAC rules cannot drift apart.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nador.extended import ExtendedSettings

# Probes that run as a DaemonSet on every node, keyed by kind, with the flags that enable them.
NODE_PROBES = {
    "cpu": ("scheduler_wait",),
    "drops": ("kernel_drops", "attributed_drops"),
    "queueing": ("network_queues",),
    "tcp": ("socket_counters", "tcp_flows"),
    "split-signaling": ("split_signaling",),
}
# Probes that run as a single Deployment.
SERVICE_PROBES = {"measurement-quality": ("quality",)}
PROBES = NODE_PROBES | SERVICE_PROBES

SPLIT_SIGNALING = "split-signaling"
MEASUREMENT_QUALITY = "measurement-quality"
RELEASE_PREFIX = "probe-"
# Flags that need extra secrets or state, so they stay off while a probe is paused.
NOT_ENABLED_WHILE_PAUSED = frozenset({"tcp_flows", "attributed_drops"})


def release_name(kind: str) -> str:
    """Helm release name of a probe kind."""
    return RELEASE_PREFIX + kind


def probe_kind(release: str) -> str:
    """Probe kind from a Helm release name (empty for non-probe releases)."""
    return release.removeprefix(RELEASE_PREFIX) if release.startswith(RELEASE_PREFIX) else ""


def workload(kind: str) -> tuple[str, str]:
    """Kubernetes resource type and name of the workload the extended-probes chart creates."""
    if kind in NODE_PROBES:
        return "daemonsets", f"{release_name(kind)}-extended-node"
    return "deployments", f"{release_name(kind)}-extended-service"


def enabled_probes(settings: "ExtendedSettings") -> list[str]:
    """Resolve probe kinds from enabled features, never from image presence alone."""
    return [name for name, flags in PROBES.items() if any(getattr(settings, flag) for flag in flags)]


def probe_values(settings: "ExtendedSettings", name: str) -> dict:
    """Mask unrelated features before serializing the release configuration."""
    data = settings.model_dump(mode="json", exclude={"probe_images"})
    for flag in {flag for flags in PROBES.values() for flag in flags}:
        data[flag] = flag in PROBES[name] and data[flag]
    if name != MEASUREMENT_QUALITY:
        data["quality_queries"] = []
    if not data["tcp_flows"]:
        data["packet_identity_secret"] = None
    data["probe_kind"] = name
    return data


def controlled_probes(settings: "ExtendedSettings") -> dict[str, bool]:
    """Return the startup state for each dedicated pinned collector."""
    return {name: any(getattr(settings, flag) for flag in PROBES[name]) for name in settings.probe_images}
