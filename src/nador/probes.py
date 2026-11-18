"""Dedicated host probes: which metrics each one collects, and its Helm release and workload names.

Every probe name and workload name is derived here, so the Python planner and the
controller's RBAC rules cannot drift apart. The flag names are the keys the probe
images read from their configuration.
"""

from typing import Any

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
# Profile metric -> probe flag. They only differ for measurement_quality.
HOST_PROBES = {flag: flag for flags in PROBES.values() for flag in flags} | {"measurement_quality": "quality"}
del HOST_PROBES["quality"]

SPLIT_SIGNALING = "split-signaling"
MEASUREMENT_QUALITY = "measurement-quality"
RELEASE_PREFIX = "probe-"
# Flags that need extra secrets or state, so they stay off while a probe is paused.
NOT_ENABLED_WHILE_PAUSED = frozenset({"tcp_flows", "attributed_drops"})


def kind_of(metric: str) -> str:
    """The probe kind that collects a host-probe metric."""
    flag = HOST_PROBES[metric]
    return next(kind for kind, flags in PROBES.items() if flag in flags)


def image_key(kind: str) -> str:
    """The images.json key of a probe kind."""
    return kind.replace("-", "_") + "_probe"


def release_name(kind: str) -> str:
    """Helm release name of a probe kind."""
    return RELEASE_PREFIX + kind


def probe_kind(release: str) -> str:
    """Probe kind from a Helm release name (empty for non-probe releases)."""
    return release.removeprefix(RELEASE_PREFIX) if release.startswith(RELEASE_PREFIX) else ""


def workload(kind: str) -> tuple[str, str]:
    """Kubernetes resource type and name of the workload the host-probes chart creates."""
    if kind in NODE_PROBES:
        return "daemonsets", f"{release_name(kind)}-node"
    return "deployments", f"{release_name(kind)}-service"


def enabled_probes(config: dict[str, Any]) -> list[str]:
    """Kinds with at least one running flag in a host-probe config."""
    return [kind for kind, flags in PROBES.items() if any(config[flag] for flag in flags)]


def probe_values(config: dict[str, Any], kind: str) -> dict[str, Any]:
    """The configuration one probe release receives: unrelated flags and settings masked."""
    data = dict(config)
    for flag in {flag for flags in PROBES.values() for flag in flags}:
        data[flag] = flag in PROBES[kind] and data[flag]
    if kind != MEASUREMENT_QUALITY:
        data["quality_queries"] = []
    if not data["tcp_flows"]:
        data["packet_identity_secret"] = None
    data["probe_kind"] = kind
    return data


def controlled_probes(config: dict[str, Any], kinds: list[str]) -> dict[str, bool]:
    """Startup state (running or paused) of each deployed probe kind."""
    return {kind: any(config[flag] for flag in PROBES[kind]) for kind in kinds}
