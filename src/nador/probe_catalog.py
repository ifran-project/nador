"""Single-purpose extended collectors and their independent release names."""

NODE_PROBES = {
    "cpu": ("scheduler_wait", "off_cpu", "off_cpu_stacks"),
    "drops": ("kernel_drops", "attributed_drops"),
    "queueing": ("network_queues",),
    "tcp": ("socket_counters", "tcp_flows"),
    "qfi": ("qfi",),
    "concordance": ("packet_concordance",),
    "split-signaling": ("split_signaling",),
    "source-liveness": (),
}
SERVICE_PROBES = {"nf-liveness": (), "measurement-quality": ("quality",)}
PROBES = NODE_PROBES | SERVICE_PROBES


def enabled_probes(settings):
    """Resolve release names from enabled features, never from image presence alone."""
    enabled = [name for name, flags in PROBES.items() if any(getattr(settings, flag) for flag in flags)]
    if any(t.source_component for t in settings.reachability):
        enabled.append("source-liveness")
    if any(not t.source_component for t in settings.reachability):
        enabled.append("nf-liveness")
    return enabled


def probe_values(settings, name):
    """Mask unrelated features before serializing the release configuration."""
    data = settings.model_dump(mode="json", exclude={"probe_images"})
    for flags in PROBES.values():
        for flag in flags:
            data[flag] = flag in PROBES[name] and data[flag]
    data["reachability"] = [
        t for t in data["reachability"]
        if (name == "source-liveness" and t["source_component"])
        or (name == "nf-liveness" and not t["source_component"])
    ]
    if name != "measurement-quality":
        data["quality_queries"] = []
    if name not in {"qfi", "tcp", "concordance"}:
        data["packet_identity_secret"] = None
    data.update(
        node_enabled=name in NODE_PROBES,
        service_enabled=name in SERVICE_PROBES,
        node_image=settings.probe_images[name] if name in NODE_PROBES else None,
        service_image=settings.probe_images[name] if name in SERVICE_PROBES else None,
        probe_kind=name,
    )
    return data
