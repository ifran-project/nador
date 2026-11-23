"""What the monitoring profile needs from the RAN, applied before ``ran.deploy()``."""

from .csi import configure_csi
from .ran_kpis import configure_ran_kpis


def configure_ran_monitoring(ran, config) -> None:
    """Prepare the RAN for the profile's ``ran_kpis`` and ``csi`` metrics, before ``ran.deploy()``.

    One call for every RAN-side setting of the profile: OCUDU's metrics stream for ``ran_kpis``
    (``configure_ran_kpis``) and the CSI logger for ``csi`` (``configure_csi``). Metrics that are off
    change nothing, so a notebook can always call it. It only updates the manager's configuration and
    refuses a RAN that is already deployed with different settings.
    """
    configure_ran_kpis(ran, config)
    configure_csi(ran, config)
