"""RFsim channel of OAI gNBs/DUs: ideal, AWGN, or 2x2 MIMO + AWGN as the 5g_ansible rfsim scenario (oai5g-rru)."""

from typing import Any

from orchestrator.deployment import RANManagement
from orchestrator.models.ran import OAIAdvancedDUConfig, OAIAdvancedGNBConfig

ADVANCED_CONFIGS = {"gnb": OAIAdvancedGNBConfig, "du": OAIAdvancedDUConfig}
# "2x2+awgn" reproduces oai5g-rru rru/gnb-config-rfsim.yaml (nb_tx/nb_rx 2, pusch_AntennaPorts 2, maxMIMO_layers 2,
# pdsch_AntennaPorts N1 2 / N2 1 / XP 1, AWGN channelmod) and its UEs (--ue-nb-ant-tx 2: 2 transmit, 1 receive antenna).
# "2x2+epa": the same antennas with the 3GPP EPA fading model (7 paths up to 410 ns, 2x2 correlation matrix), which
# couples the antennas and makes the SRS estimates frequency selective; rfsim draws the channel once per UE
# connection (OAI develop simulator.cpp), and the multipath convolution is much more CPU-intensive than AWGN.
# Known issues with OAI develop-f80a883 (2026-10-16), observed on SLICES BI:
# - "2x2+epa" is still to be debugged: the gNB/DU softmodem crashes as soon as a UE connects, right after
#   "Random channel rfsimu_channel_ue0 in rfsimulator activated" (ru_thread: NULL pointer read in librfsimulator.so,
#   always at offset 0xf11b), so no UE attaches.
# - "2x2+awgn": UEs attach, but a UE softmodem may crash under sustained traffic (DL__actor thread: invalid write in
#   libc.so.6, always at offset 0x17173a).
RFSIM_CHANNELS = ("2x2+awgn", "2x2+epa", "awgn", "ideal")
# Read by the nador OAI config.yaml.j2, which renders the 2x2 antenna settings of the gNB/DU (also set in the
# container environment, where OAI ignores it).
ANTENNAS_MARKER = "RFSIM_ANTENNAS"
# UE antennas (transmit, receive) per channel. The OAI UE takes them from the RUs section of its configuration, which
# the orchestrator renders from the simulated RU shared with the gNB/DU (--ue-nb-ant-tx is overridden by it); the
# gNB/DU antennas come from the marker above. With one UE transmit antenna, a 2x2 gNB never gets the RAR through.
UE_ANTENNAS = {"2x2+awgn": (2, 1), "2x2+epa": (2, 1), "awgn": (1, 1), "ideal": (1, 1)}
CHANNEL_MODELS = {"2x2+awgn": "AWGN", "2x2+epa": "EPA", "awgn": "AWGN"}

__all__ = ["RFSIM_CHANNELS", "enable_rfsim_channel_model", "set_rfsim_channel"]


def set_rfsim_channel(
    ran: RANManagement,
    channel: str = "2x2+awgn",
    components: list[str] | None = None,
    *,
    noise_power_dB: float = -30,  # noqa: N803 (OAI parameter name)
    ploss_dB: float = 0,  # noqa: N803 (OAI parameter name)
) -> None:
    """
    Select the RFsim channel of the OAI gnb/du components with a simulated RU and of their OAI UEs (before prepare).

    "2x2+awgn" (default): 2x2 MIMO (2 gNB/DU antennas, 2 PUSCH ports, 2 MIMO layers; UEs with 2 transmit antennas
    and 1 receive antenna) and the AWGN channel model, as the 5g_ansible rfsim scenario. "awgn": the same AWGN
    channel model with one antenna. "ideal": no channel model, one antenna. "2x2+epa" (still to be debugged, see
    the known issues above RFSIM_CHANNELS): the 2x2 antennas with the 3GPP EPA fading model, for frequency-selective
    SRS estimates on both gNB/DU antennas, fixed for a UE connection, at a much higher CPU cost. The UE antennas
    are set on the simulated RU of each radio in the metamodel. Call it before the radios are prepared and before
    the UE manager is created (UEManagement copies the RAN metamodel and deployment model). OAI's AWGN channel is
    the identity matrix plus noise: with the single-port SRS of the UEs, the second gNB/DU antenna receives no SRS.
    """
    if channel not in RFSIM_CHANNELS:
        raise ValueError(f"channel must be one of {RFSIM_CHANNELS}")
    radios = _rfsim_radios(ran, components)
    model = ran.deployment_model.ran.components
    for name in radios:
        component = model[name]
        env = {k: v for k, v in (component.extra_env_vars or {}).items() if k != ANTENNAS_MARKER}
        if channel.startswith("2x2"):
            env[ANTENNAS_MARKER] = "2"
        component.extra_env_vars = env
        if channel == "ideal" and component.advanced_config is not None:
            component.advanced_config = component.advanced_config.model_copy(
                update={"rfsimulator": None, "channelmod": None}
            )
    if channel != "ideal":
        enable_rfsim_channel_model(
            ran, radios, model_type=CHANNEL_MODELS[channel], noise_power_dB=noise_power_dB, ploss_dB=ploss_dB
        )
    _set_ru_ports(ran, radios, *UE_ANTENNAS[channel])


def _rfsim_radios(ran: RANManagement, components: list[str] | None) -> list[str]:
    """OAI gnb/du components with a simulated RU (all of them by default; named ones must qualify)."""
    model = ran.deployment_model.ran.components
    names = list(model) if components is None else components
    unknown = set(names) - model.keys()
    if unknown:
        raise ValueError(f"Unknown RAN components: {sorted(unknown)}")
    radios = []
    for name in names:
        component = model[name]
        # type first: CU, CU-CP and CU-UP have no RU
        supported = str(component.implementation) == "oai" and component.type in ADVANCED_CONFIGS
        if not supported or str(ran.mutable_metamodel.ran.components[name].config.ru.type) != "simulated":
            if components is None:
                continue
            raise ValueError(f"{name}: the rfsim channel applies to OAI gnb/du components with a simulated RU")
        radios.append(name)
    return radios


def _set_ru_ports(ran: RANManagement, radios: list[str], tx: int, rx: int) -> None:
    """Set the ports of the simulated RUs of the radios (hence of their UEs) in the RAN metamodel."""
    rus = {ran.metamodel.ran.components[name].config.ru for name in radios}
    for ru in rus:
        ran.metamodel.rus[ru].numberOfTxPorts = tx
        ran.metamodel.rus[ru].numberOfRxPorts = rx
    if rus:
        ran.mutable_metamodel = ran.metamodel.model_copy(deep=True)
        ran.reconcile()


def enable_rfsim_channel_model(
    ran: RANManagement,
    components: list[str] | None = None,
    *,
    model_type: str = "AWGN",
    noise_power_dB: float = -30,  # noqa: N803 (OAI parameter name)
    ploss_dB: float = 0,  # noqa: N803 (OAI parameter name)
    ue_channels: int = 3,
) -> None:
    """
    Enable the rfsimulator channel model ("chanmod") on OAI gnb/du components with a simulated RU (before prepare).

    Defaults reproduce 5g_ansible: an AWGN channel, noise power -30 dB, no path loss, applied by the gNB/DU to what it
    receives (uplink, hence the SRS used for CSI). OAI adds 10**(noise_power_dB/10) * 256 of noise per int16 sample:
    -30 dB is negligible, 0 dB already prevents UE attachment. The gNB/DU reads rfsimu_channel_ue<i> for its i-th
    connected UE and falls back to rfsimu_channel_ue0 beyond ue_channels; rfsimu_channel_enB0 is the UE-side model
    (unused unless the UEs enable chanmod too). The upstream template renders advanced_config.rfsimulator as a
    mapping, which makes OAI 2026.w42 crash when it opens the RF device: the nador OAI config.yaml.j2 rewrites it
    as a list.
    """
    model = ran.deployment_model.ran.components
    channel = {
        "type": model_type,
        "ploss_dB": ploss_dB,
        "noise_power_dB": noise_power_dB,
        "forgetfact": 0.0,
        "offset": 0,
        "ds_tdl": 0,
    }
    model_names = ["rfsimu_channel_enB0", *(f"rfsimu_channel_ue{i}" for i in range(ue_channels))]
    channelmod = {
        "max_chan": 10,
        "modellist": "modellist_rfsimu_1",
        "modellist_rfsimu_1": [{"model_name": name, **channel} for name in model_names],
    }
    for name in _rfsim_radios(ran, components):
        component = model[name]
        advanced = component.advanced_config or ADVANCED_CONFIGS[component.type]()
        rfsimulator: dict[str, Any] = {"serveraddr": "server", "serverport": 4043} | (advanced.rfsimulator or {})
        options = list(rfsimulator.get("options") or [])
        rfsimulator |= {
            "options": options if "chanmod" in options else [*options, "chanmod"],
            "modelname": model_type,
            "IQfile": "/tmp/rfsimulator.iqs",  # noqa: S108 (5g_ansible value, in the container; only written with "saviq")
        }
        component.advanced_config = advanced.model_copy(update={"rfsimulator": rfsimulator, "channelmod": channelmod})
