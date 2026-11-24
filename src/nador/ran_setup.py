"""What the monitoring profile needs from the RAN, applied before ``ran.deploy()``."""

from .csi import configure_csi
from .ran_kpis import configure_ran_kpis

OCUDU_LOG_LEVELS = ("none", "error", "warning", "info", "debug")  # from least to most detailed
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

    With ``ran.logs`` on, every OCUDU component must log to ``stdout``, where Loki reads it (OCUDU's default is a
    file in the pod). With the RNTI mapper (``ran_kpis`` or ``csi`` on), each gNB, CU and CU-CP must log to
    ``stdout`` with its CU and NGAP lines at ``info`` or ``debug``: the mapper names the UE behind each RNTI from
    them. ``cu_level`` and ``ngap_level`` take ``all_level`` when not set, as OCUDU does. Components without their
    own ``log`` keep IFRAN's, which fits.
    """
    metrics = config.metrics
    logs, mapper = metrics.ran_logs is not None, bool(metrics.ran_kpis or metrics.csi is not None)
    if not (logs or mapper):
        return
    problems = []
    for name, component in ran.deployment_model.ran.components.items():
        log = _ocudu_log(component)
        if log is not None:
            reads_ues = mapper and str(component.type) in RRC_TYPES
            problems += _log_problems(name, log, to_stdout=logs or reads_ues, ue_lines=reads_ues)
    if problems:
        listed = "\n  - ".join(problems)
        raise ValueError(f"OCUDU log settings hide what monitoring reads (see the Logs guide):\n  - {listed}")


def _ocudu_log(component) -> dict | None:
    """The component's own OCUDU log section, or None (not OCUDU, or IFRAN's log section)."""
    if str(component.implementation) != "ocudu" or component.advanced_config is None:
        return None
    return getattr(component.advanced_config, "log", None)


def _log_problems(name: str, log: dict, *, to_stdout: bool, ue_lines: bool) -> list[str]:
    """What in one log section hides the lines monitoring reads."""
    problems = []
    if to_stdout and log.get("filename") != "stdout":
        problems.append(f"{name}: log.filename must be stdout (OCUDU's default is a file in the pod)")
    for layer in ("cu", "ngap") if ue_lines else ():
        level = str(log.get(f"{layer}_level", log.get("all_level", "warning")))
        if level not in OCUDU_LOG_LEVELS:
            levels = ", ".join(OCUDU_LOG_LEVELS)
            problems.append(f"{name}: unknown log level {level!r} for {layer}; levels: {levels}")
        elif OCUDU_LOG_LEVELS.index(level) < OCUDU_LOG_LEVELS.index("info"):
            problems.append(f"{name}: log.{layer}_level must be info or debug for the RNTI mapper, not {level}")
    return problems
