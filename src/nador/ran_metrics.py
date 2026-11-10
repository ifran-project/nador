"""Configure and resolve implementation-native RAN telemetry."""

from orchestrator.utils.k8s import build_k8s_safe


OCUDU_LAYERS = {
    "gnb": ("app_usage", "e1ap", "pdcp", "sched", "rlc", "mac", "executor", "du_low", "ru"),
    "cu": ("app_usage",),
    "cu_cp": ("app_usage",),
    "cu_up": ("app_usage", "pdcp"),
    "du": ("app_usage", "sched", "mac", "rlc", "du_low", "ru"),
}


def configure_ran_kpis(ran, config) -> None:
    """Enable OCUDU's native JSON stream before ``ran.deploy()``.

    Existing OAI parser configuration is left unchanged. This only updates the
    supplied manager's configuration; it does not contact or restart a cluster.
    """
    if not config.ran_kpis:
        return
    for name, component in ran.deployment_model.ran.components.items():
        if str(component.implementation) != "ocudu":
            continue
        advanced = component.advanced_config
        data = advanced.model_dump(exclude_none=True) if advanced is not None else {}
        original = data.copy()
        metrics = dict(data.get("metrics") or {})
        layers = dict(metrics.get("layers") or {})
        for layer in OCUDU_LAYERS[str(component.type)]:
            layers.setdefault(f"enable_{layer}", True)
        metrics.update(enable_json=True, layers=layers)
        remote = dict(data.get("remote_control") or {})
        remote.update(enabled=True, bind_addr="0.0.0.0", port=config.ocudu_metrics_port)
        data.update(metrics=metrics, remote_control=remote)
        if component.is_installed:
            if data != original:
                raise ValueError(f"{name}: configure RAN KPIs before deploying the RAN")
            continue
        validated = type(component).model_validate(component.model_dump() | {"advanced_config": data})
        component.advanced_config = validated.advanced_config


def ocudu_sources(model) -> list[dict]:
    """Resolve every OCUDU radio component, not just packet-capture targets."""
    if not model.monitoring.ran_kpis:
        return []
    sources = []
    for name, component in model.ran.components.items():
        if str(component.implementation) != "ocudu":
            continue
        if not component.namespace:
            raise ValueError(f"{name}: RAN namespace is unresolved")
        advanced = component.advanced_config
        remote = (advanced.remote_control if advanced is not None else None) or {}
        metrics = (advanced.metrics if advanced is not None else None) or {}
        if not metrics.get("enable_json") or not remote.get("enabled"):
            raise ValueError(f"{name}: call configure_ran_kpis(ran, config) before ran.deploy()")
        if remote.get("bind_addr", "127.0.0.1") in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(f"{name}: OCUDU metrics must bind to the pod network, not loopback")
        target = next((t for t in model.monitoring.targets if t.category == "ran" and t.component == name), None)
        sources.append({
            "component": name,
            "kind": str(component.type),
            "namespace": component.namespace,
            "selector": (target.selector if target else None) or {"app": build_k8s_safe(f"ocudu-{name}")},
            "source_service": build_k8s_safe(f"ocudu-source-{name}"),
            "exporter": build_k8s_safe(f"ocudu-metrics-{name}"),
            "port": remote.get("port", 8001),
        })
    if str(model.ran.implementation) == "ocudu" and not sources:
        raise ValueError("ran_kpis requires at least one deployed OCUDU RAN component")
    return sources
