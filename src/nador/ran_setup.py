"""What the monitoring profile needs from the RAN, applied before ``ran.deploy()``."""

from .csi import configure_csi
from .ran_kpis import configure_ran_kpis

# OCUDU's log levels, from least to most detailed; its default filename is a file in the pod.
OCUDU_LOG_LEVELS = ("none", "error", "warning", "info", "debug")
OCUDU_DEFAULT_LOG_FILE = "/tmp/gnb.log"  # noqa: S108 - OCUDU's default, compared against, never opened
RRC_TYPES = ("gnb", "cu", "cu_cp")  # the RNTI mapper reads the UE contexts in their logs


def configure_ran_monitoring(ran, config) -> None:
    """Prepare the RAN for the profile's ``ran_kpis`` and ``csi`` metrics, before ``ran.deploy()``.

    One call for every RAN-side setting of the profile: OCUDU's metrics stream for ``ran_kpis``
    (``configure_ran_kpis``) and the CSI logger for ``csi`` (``configure_csi``). Metrics that are off
    change nothing, so a notebook can always call it. It only updates the manager's configuration and
    refuses a RAN that is already deployed with different settings, or whose OCUDU log settings would hide
    what the profile reads (``check_ran_logs``).
    """
    check_ran_logs(ran, config)
    configure_ran_kpis(ran, config)
    configure_csi(ran, config)


def check_ran_logs(ran, config) -> None:
    """Refuse OCUDU log settings (``advanced_config.log``) that would hide what the profile reads from the logs.

    With ``ran.logs`` on, every OCUDU component must log to ``stdout``, where Loki reads it. With the RNTI mapper
    (``ran_kpis`` or ``csi`` on), each gNB, CU and CU-CP must log to ``stdout`` with its CU and NGAP lines at
    ``info`` or ``debug``: the mapper names the UE behind each RNTI from them. ``cu_level`` and ``ngap_level`` take
    ``all_level`` when not set, as OCUDU does. Components without their own ``log`` keep IFRAN's, which fits.
    """
    metrics = config.metrics
    logs, mapper = metrics.ran_logs is not None, bool(metrics.ran_kpis or metrics.csi is not None)
    if not (logs or mapper):
        return
    problems = []
    for name, component in ran.deployment_model.ran.components.items():
        advanced = component.advanced_config
        log = getattr(advanced, "log", None) if advanced is not None else None
        if str(component.implementation) != "ocudu" or log is None:
            continue
        reads_ues = mapper and str(component.type) in RRC_TYPES
        if (logs or reads_ues) and log.get("filename", OCUDU_DEFAULT_LOG_FILE) != "stdout":
            problems.append(f"{name}: log.filename must be stdout (OCUDU's default is a file in the pod)")
        if not reads_ues:
            continue
        for layer in ("cu", "ngap"):
            level = str(log.get(f"{layer}_level", log.get("all_level", "warning")))
            if level not in OCUDU_LOG_LEVELS:
                problems.append(
                    f"{name}: unknown log level {level!r} for {layer}; levels: {', '.join(OCUDU_LOG_LEVELS)}"
                )
            elif OCUDU_LOG_LEVELS.index(level) < OCUDU_LOG_LEVELS.index("info"):
                problems.append(f"{name}: log.{layer}_level must be info or debug for the RNTI mapper, not {level}")
    if problems:
        raise ValueError(
            "OCUDU log settings hide what monitoring reads (see the Logs guide):\n  - " + "\n  - ".join(problems)
        )
