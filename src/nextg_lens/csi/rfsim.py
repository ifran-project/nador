"""RFsim channel of OAI gNBs/DUs: ideal, AWGN, or 2x2 MIMO + AWGN as the 5g_ansible rfsim scenario (oai5g-rru)."""

from typing import Any

from orchestrator.deployment import RANManagement
from orchestrator.models.ran import OAIAdvancedDUConfig, OAIAdvancedGNBConfig
from orchestrator.models.ue import OAIAdvancedUEConfig

ADVANCED_CONFIGS = {"gnb": OAIAdvancedGNBConfig, "du": OAIAdvancedDUConfig}
# "2x2+awgn" reproduces oai5g-rru rru/gnb-config-rfsim.yaml (nb_tx/nb_rx 2, pusch_AntennaPorts 2, maxMIMO_layers 2,
# pdsch_AntennaPorts N1 2 / N2 1 / XP 1, AWGN channelmod) and its UE option --ue-nb-ant-tx 2.
RFSIM_CHANNELS = ("2x2+awgn", "awgn", "ideal")
# Read by the nextg-lens OAI config.yaml.j2, which renders the 2x2 antenna settings of the gNB/DU (also set in the
# container environment, where OAI ignores it).
ANTENNAS_MARKER = "RFSIM_ANTENNAS"
UE_TX_ANTENNAS_OPTION = "--ue-nb-ant-tx 2"
UE_DEFAULT_OPTIONS = "-E --rfsim"  # orchestrator default for an OAI UE on an OAI simulated RU

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

    "2x2+awgn" (default): 2x2 MIMO (2 gNB/DU antennas, 2 PUSCH ports, 2 MIMO layers, UEs transmitting on 2 antennas)
    and the AWGN channel model, as the 5g_ansible rfsim scenario. "awgn": the same AWGN channel model with one
    antenna. "ideal": no channel model, one antenna. Call it before the radios are prepared and before the UE
    manager is created (UEManagement copies the UE settings of the RAN deployment model).
    """
    if channel not in RFSIM_CHANNELS:
        raise ValueError(f"channel must be one of {RFSIM_CHANNELS}")
    radios = _rfsim_radios(ran, components)
    model = ran.deployment_model.ran.components
    for name in radios:
        component = model[name]
        env = {k: v for k, v in (component.extra_env_vars or {}).items() if k != ANTENNAS_MARKER}
        if channel == "2x2+awgn":
            env[ANTENNAS_MARKER] = "2"
        component.extra_env_vars = env
        if channel == "ideal" and component.advanced_config is not None:
            component.advanced_config = component.advanced_config.model_copy(
                update={"rfsimulator": None, "channelmod": None}
            )
    if channel != "ideal":
        enable_rfsim_channel_model(ran, radios, noise_power_dB=noise_power_dB, ploss_dB=ploss_dB)
    _set_ue_tx_antennas(ran, radios, two=channel == "2x2+awgn")


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


def _set_ue_tx_antennas(ran: RANManagement, radios: list[str], *, two: bool) -> None:
    """Add or remove --ue-nb-ant-tx 2 on the OAI UEs attached to the given radios."""
    for name, ue in (ran.deployment_model.ues or {}).items():
        meta = ran.mutable_metamodel.ues[name]
        if str(ue.implementation) != "oai" or not set(meta.config.gnbs) & set(radios):
            continue
        advanced = ue.advanced_config or OAIAdvancedUEConfig()
        options = (advanced.useAdditionalOptions or UE_DEFAULT_OPTIONS).replace(UE_TX_ANTENNAS_OPTION, "").split()
        if two:
            options += UE_TX_ANTENNAS_OPTION.split()
        joined = " ".join(options)
        ue.advanced_config = advanced.model_copy(
            update={"useAdditionalOptions": None if joined == UE_DEFAULT_OPTIONS else joined}
        )


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
    mapping, which makes OAI 2026.w42 crash when it opens the RF device: the nextg-lens OAI config.yaml.j2 rewrites it
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
