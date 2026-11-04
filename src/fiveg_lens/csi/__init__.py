"""CSI logging (oai-csi-logging images) for OAI gNBs deployed with the fiveg-lens OAI charts."""

from orchestrator.deployment import RANManagement
from orchestrator.models.k8s import ContainerImage

# Upstream default OAI gNB repository -> CSI logger image built from the same OAI tag.
CSI_IMAGE_REPOSITORIES = {
    "docker.io/oaisoftwarealliance/oai-gnb": "docker.io/r2labuser/oai-gnb-csi",
}

DEFAULT_OUTPUT_DIR = "/data/csi"

__all__ = ["DEFAULT_OUTPUT_DIR", "enable_csi"]


def enable_csi(
    ran: RANManagement,
    components: list[str] | None = None,
    image: ContainerImage | None = None,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    env: dict[str, str] | None = None,
) -> None:
    """
    Select the CSI logger image and its environment on OAI gNB components, before ran.prepare().

    The fiveg-lens OAI charts (custom_chart_path / custom_rendered_files) must already be selected:
    they mount output_dir as a hostPath and force do_SRS: periodic / do_CSIRS: 1 when
    CSI_LOGGING_ENABLED is "1". env adds or overrides CSI_* variables (CSI_GRANULARITY,
    CSI_ANTENNA_SELECTION, CSI_PORT_SELECTION, CSI_SUBCARRIER_SAMPLING, CSI_FLUSH_CORE).
    """
    all_components = ran.deployment_model.ran.components
    names = list(all_components) if components is None else components
    unknown = set(names) - all_components.keys()
    if unknown:
        raise ValueError(f"Unknown RAN components: {sorted(unknown)}")

    for name in names:
        component = all_components[name]
        if component.implementation != "oai" or component.type != "gnb":
            # Only the monolithic gNB chart mounts the CSI directory for now.
            if components is None:
                continue
            raise ValueError(f"{name}: CSI logging is only supported on OAI gnb components")
        if not component.custom_chart_path:
            raise ValueError(f"{name}: select the fiveg-lens OAI charts before enabling CSI")

        if image is not None:
            component.image = image.model_copy(deep=True)
        elif component.image is not None and not component.image.repository.endswith("-csi"):
            repository = CSI_IMAGE_REPOSITORIES.get(component.image.repository)
            if repository is None:
                raise ValueError(f"{name}: no CSI image known for {component.image.repository!r}; pass image=")
            component.image = component.image.model_copy(update={"repository": repository})

        component.extra_env_vars = (
            (component.extra_env_vars or {})
            | {
                "CSI_LOGGING_ENABLED": "1",
                "CSI_OUTPUT_DIR": output_dir,
            }
            | (env or {})
        )
