"""Extended collectors and their independent release names."""

NODE_PROBES = {
    "cpu": ("scheduler_wait",),
    "drops": ("kernel_drops", "attributed_drops"),
    "queueing": ("network_queues",),
    "tcp": ("socket_counters", "tcp_flows"),
    "split-signaling": ("split_signaling",),
}
SERVICE_PROBES = {"measurement-quality": ("quality",)}
PROBES = NODE_PROBES | SERVICE_PROBES


def enabled_probes(settings):
    """Resolve release names from enabled features, never from image presence alone."""
    return [
        name for name, flags in PROBES.items()
        if any(getattr(settings, flag) for flag in flags)
    ]


def probe_values(settings, name):
    """Mask unrelated features before serializing the release configuration."""
    data = settings.model_dump(mode="json", exclude={"probe_images"})
    for flag in {flag for flags in PROBES.values() for flag in flags}:
        data[flag] = flag in PROBES[name] and data[flag]
    if name != "measurement-quality":
        data["quality_queries"] = []
    if not data["tcp_flows"]:
        data["packet_identity_secret"] = None
    data.update(
        node_enabled=name in NODE_PROBES,
        service_enabled=name in SERVICE_PROBES,
        node_image=settings.probe_images[name] if name in NODE_PROBES else None,
        service_image=settings.probe_images[name] if name in SERVICE_PROBES else None,
        probe_kind=name,
    )
    return data


def controlled_probes(settings):
    """Return the startup state for each dedicated pinned collector."""
    return {name: any(getattr(settings, flag) for flag in PROBES[name]) for name in settings.probe_images}
