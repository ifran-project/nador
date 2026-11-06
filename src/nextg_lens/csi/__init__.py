"""CSI logging (oai-csi-logging images) for OAI gNBs deployed with the nextg-lens OAI charts."""

from orchestrator.deployment import RANManagement
from orchestrator.models.k8s import ContainerImage

from nextg_lens.csi.collection import CsiCollector
from nextg_lens.csi.live import CsiLiveView
from nextg_lens.csi.storage import prepare_csi_storage

# Upstream default OAI gNB repository -> CSI logger image built from the same OAI tag.
CSI_IMAGE_REPOSITORIES = {
    "docker.io/oaisoftwarealliance/oai-gnb": "docker.io/r2labuser/oai-gnb-csi",
}

DEFAULT_OUTPUT_DIR = "/data/csi"
CSI_COMPONENT_TYPES = ("gnb", "du")

__all__ = [
    "DEFAULT_OUTPUT_DIR",
    "CsiCollector",
    "CsiLiveView",
    "enable_csi",
    "prepare_csi_storage",
    "select_csi_images",
]


def enable_csi(
    ran: RANManagement,
    components: list[str] | None = None,
    image: ContainerImage | None = None,
    output_dir: str = DEFAULT_OUTPUT_DIR,
    env: dict[str, str] | None = None,
) -> None:
    """
    Select the CSI logger image and its environment on OAI gnb / du components, before ran.prepare().

    The nextg-lens OAI charts (custom_chart_path / custom_rendered_files) must already be selected:
    they mount output_dir as a hostPath and force do_SRS: periodic / do_CSIRS: 1 when
    CSI_LOGGING_ENABLED is "1". env adds or overrides CSI_* variables (CSI_GRANULARITY,
    CSI_ANTENNA_SELECTION, CSI_PORT_SELECTION, CSI_SUBCARRIER_SAMPLING, CSI_FLUSH_CORE).
    Each logging component writes <output_dir>/csi_per_rb.csv on its node: two of them on the same
    node need different output_dir values (call enable_csi once per component).
    The other OAI components get their image without CSI logger (see select_csi_images): call
    enable_csi(ran, components=[]) for a deployment without CSI.
    """
    all_components = ran.deployment_model.ran.components
    names = list(all_components) if components is None else components
    unknown = set(names) - all_components.keys()
    if unknown:
        raise ValueError(f"Unknown RAN components: {sorted(unknown)}")

    selected = []
    for name in names:
        component = all_components[name]
        if component.implementation != "oai" or component.type not in CSI_COMPONENT_TYPES:
            # Only the gnb and du charts mount the CSI directory (the CU has no PHY, hence no SRS).
            if components is None:
                continue
            raise ValueError(f"{name}: CSI logging is only supported on OAI gnb and du components")
        if not component.custom_chart_path:
            raise ValueError(f"{name}: select the nextg-lens OAI charts before enabling CSI")
        selected.append(name)
    _check_distinct_files(all_components, selected, output_dir)

    for name in selected:
        component = all_components[name]

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
    select_csi_images(ran)


def _check_distinct_files(all_components: dict, selected: list[str], output_dir: str) -> None:
    """Refuse two CSI loggers writing the same hostPath file (same node, same output directory)."""
    files = {}
    for name, component in all_components.items():
        env = component.extra_env_vars or {}
        if name in selected:
            directory = output_dir
        elif str(env.get("CSI_LOGGING_ENABLED")) == "1":
            directory = env.get("CSI_OUTPUT_DIR", DEFAULT_OUTPUT_DIR)
        else:
            continue
        files.setdefault(directory.rstrip("/"), []).append((name, component.node_name))
    for directory, users in files.items():
        if len(users) == 1:
            continue
        if any(node is None for _, node in users):
            # Without node_name the scheduler may put them on the same node.
            raise ValueError(
                f"{[n for n, _ in users]} share {directory}: set node_name on each or use distinct output_dir"
            )
        nodes = [node for _, node in users]
        shared = {node for node in nodes if nodes.count(node) > 1}
        if shared:
            raise ValueError(
                f"{[n for n, node in users if node in shared]} write {directory}/csi_per_rb.csv on the same node: "
                "use distinct output_dir"
            )


def select_csi_images(ran: RANManagement) -> None:
    """
    Use a CSI logger image only on the components where CSI logging is enabled.

    The logger of these images always starts (oai-csi-logging v3.1), and the orchestrator defaults to
    r2labuser/oai-gnb-aw2s-csi for the AW2S gNB, DU, CU and CU-CP: without enable_csi it would write
    /data/csi inside the container. Every other OAI component with a "-csi" image gets the same image
    without logger: the reverse of CSI_IMAGE_REPOSITORIES, else the repository without its "-csi" suffix
    (r2labuser/oai-gnb-aw2s-csi -> r2labuser/oai-gnb-aw2s), with the same tag.
    """
    without_csi = {csi: plain for plain, csi in CSI_IMAGE_REPOSITORIES.items()}
    for component in ran.deployment_model.ran.components.values():
        if component.implementation != "oai" or component.image is None:
            continue
        if str((component.extra_env_vars or {}).get("CSI_LOGGING_ENABLED")) == "1":
            continue
        repository = component.image.repository
        if repository.endswith("-csi"):
            plain = without_csi.get(repository, repository.removesuffix("-csi"))
            component.image = component.image.model_copy(update={"repository": plain})
