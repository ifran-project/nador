"""RFsim channel model of OAI gNBs/DUs, as in 5g_ansible (oai5g-rru rru/gnb-config-rfsim.yaml, du-config-rfsim.yaml)."""

from typing import Any

from orchestrator.deployment import RANManagement
from orchestrator.models.ran import OAIAdvancedDUConfig, OAIAdvancedGNBConfig

ADVANCED_CONFIGS = {"gnb": OAIAdvancedGNBConfig, "du": OAIAdvancedDUConfig}

__all__ = ["enable_rfsim_channel_model"]


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
    receives (uplink, hence the SRS used for CSI). The gNB/DU reads rfsimu_channel_ue<i> for its i-th connected UE
    and falls back to rfsimu_channel_ue0 beyond ue_channels; rfsimu_channel_enB0 is the UE-side model (unused unless
    the UEs enable chanmod too). The upstream template renders advanced_config.rfsimulator as a mapping: OAI reads it
    and logs "rfsimulator parameters should be provided as array elements".
    """
    all_components = ran.deployment_model.ran.components
    names = list(all_components) if components is None else components
    unknown = set(names) - all_components.keys()
    if unknown:
        raise ValueError(f"Unknown RAN components: {sorted(unknown)}")

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
    for name in names:
        component = all_components[name]
        # type first: CU, CU-CP and CU-UP have no RU
        supported = str(component.implementation) == "oai" and component.type in ADVANCED_CONFIGS
        meta = ran.mutable_metamodel.ran.components[name]
        if not supported or str(meta.config.ru.type) != "simulated":
            if components is None:
                continue
            raise ValueError(f"{name}: the rfsim channel model applies to OAI gnb/du components with a simulated RU")
        advanced = component.advanced_config or ADVANCED_CONFIGS[component.type]()
        rfsimulator: dict[str, Any] = {"serveraddr": "server", "serverport": 4043} | (advanced.rfsimulator or {})
        options = list(rfsimulator.get("options") or [])
        rfsimulator |= {
            "options": options if "chanmod" in options else [*options, "chanmod"],
            "modelname": model_type,
            "IQfile": "/tmp/rfsimulator.iqs",  # noqa: S108 (5g_ansible value, in the container; only written with "saviq")
        }
        component.advanced_config = advanced.model_copy(update={"rfsimulator": rfsimulator, "channelmod": channelmod})
