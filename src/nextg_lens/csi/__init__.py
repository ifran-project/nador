"""CSI logging of OAI gNBs/DUs (oai-csi-logging images) and OCUDU gNBs (ocudu-csi-logging images)."""

from importlib.resources import files

from metamodel.models.ran import RANComponentType
from orchestrator.deployment import RANManagement
from orchestrator.models.k8s import ContainerImage
from orchestrator.models.provisioning import RUVendor
from orchestrator.models.ran import RANImplementation
from orchestrator.utils.defaults import DEFAULT_RAN_IMAGES

from nextg_lens.csi.collection import CsiCollector
from nextg_lens.csi.live import CsiLiveView
from nextg_lens.csi.rfsim import RFSIM_CHANNELS, enable_rfsim_channel_model, set_rfsim_channel
from nextg_lens.csi.storage import prepare_csi_storage

# Upstream default OAI gNB repository -> CSI logger image built from the same OAI tag.
CSI_IMAGE_REPOSITORIES = {
    "docker.io/oaisoftwarealliance/oai-gnb": "docker.io/r2labuser/oai-gnb-csi",
}

# OCUDU gNB: one CSI image per radio variant (ocudu-csi-logging build_image.sh), matched to the upstream
# default gNB image of that variant (orchestrator DEFAULT_RAN_IMAGES).
OCUDU_CSI_REPOSITORY = "docker.io/r2labuser/ocudu-gnb-csi"
OCUDU_CSI_TAGS = {None: "release_26_10-zmq", RUVendor.USRP: "release_26_10-uhd", RUVendor.BENETEL: "release_26_10"}
OCUDU_SRS_PERIODS_MS = (1, 2, 2.5, 4, 5, 8, 10, 16, 20, 32, 40, 64, 80, 160, 320, 640, 1280, 2560)
OCUDU_TEMPLATES = files("nextg_lens") / "assets" / "ran" / "ocudu"

DEFAULT_OUTPUT_DIR = "/data/csi"
# The upstream OCUDU gNB chart mounts only the node's /mnt (hostPath): the OCUDU logger writes there.
OCUDU_OUTPUT_DIR = "/mnt/csi"
# OAI gnb/du and OCUDU gnb: no CSI image for the OCUDU DU, and the CU has no PHY, hence no SRS.
CSI_COMPONENT_TYPES = {"oai": ("gnb", "du"), "ocudu": ("gnb",)}

__all__ = [
    "DEFAULT_OUTPUT_DIR",
    "OCUDU_OUTPUT_DIR",
    "CsiCollector",
    "CsiLiveView",
    "enable_csi",
    "enable_rfsim_channel_model",
    "set_rfsim_channel",
    "RFSIM_CHANNELS",
    "prepare_csi_storage",
    "select_csi_images",
]


def enable_csi(
    ran: RANManagement,
    components: list[str] | None = None,
    image: ContainerImage | None = None,
    output_dir: str | None = None,
    env: dict[str, str] | None = None,
    *,
    srs_period_ms: float = 40,
) -> None:
    """
    Select the CSI logger image and environment of OAI gnb/du and OCUDU gnb components (before ran.prepare()).

    OAI: the nextg-lens OAI charts (custom_chart_path / custom_rendered_files) must already be selected:
    they mount output_dir (default /data/csi) as a hostPath and force do_SRS: periodic / do_CSIRS: 1 when
    CSI_LOGGING_ENABLED is "1". OCUDU: the upstream gNB chart is kept with the nextg-lens rendered files
    (selected here), which add cell_cfg.srs (periodic, srs_period_ms); the logger writes under the node's
    /mnt, the only hostPath of that chart (output_dir default /mnt/csi).
    env adds or overrides CSI_* variables (CSI_GRANULARITY, CSI_ANTENNA_SELECTION, CSI_PORT_SELECTION,
    CSI_SUBCARRIER_SAMPLING, CSI_FLUSH_CORE).
    Each logging component writes <output_dir>/csi_per_rb.csv on its node: two of them on the same
    node need different output_dir values (call enable_csi once per component).
    The other components get their image without CSI logger (see select_csi_images): call
    enable_csi(ran, components=[]) for a deployment without CSI.
    """
    all_components = ran.deployment_model.ran.components
    names = list(all_components) if components is None else components
    unknown = set(names) - all_components.keys()
    if unknown:
        raise ValueError(f"Unknown RAN components: {sorted(unknown)}")
    if srs_period_ms not in OCUDU_SRS_PERIODS_MS:
        raise ValueError(f"srs_period_ms must be an OCUDU SRS period (ms): {OCUDU_SRS_PERIODS_MS}")

    directories = _csi_directories(all_components, names, output_dir, explicit=components is not None)
    _check_distinct_files(all_components, directories)

    for name, directory in directories.items():
        component = all_components[name]
        ocudu = str(component.implementation) == "ocudu"
        _select_image(name, component, image, ocudu=ocudu)
        csi_env = {"CSI_LOGGING_ENABLED": "1", "CSI_OUTPUT_DIR": directory}
        if ocudu:
            # CSI_ENABLED switches the OCUDU logger on; CSI_SRS_PERIOD_MS is read by the rendered configuration.
            csi_env |= {"CSI_ENABLED": "1", "CSI_SRS_PERIOD_MS": f"{srs_period_ms:g}"}
            component.custom_rendered_files = _ocudu_rendered_files()
        component.extra_env_vars = (component.extra_env_vars or {}) | csi_env | (env or {})
    select_csi_images(ran)


def _csi_directories(all_components: dict, names: list[str], output_dir: str | None, *, explicit: bool) -> dict:
    """CSI output directory of each selected component; unsupported components are skipped unless named."""
    directories = {}
    for name in names:
        component = all_components[name]
        implementation = str(component.implementation)
        if component.type not in CSI_COMPONENT_TYPES.get(implementation, ()):
            if not explicit:
                continue
            raise ValueError(f"{name}: CSI logging is only supported on OAI gnb/du and OCUDU gnb components")
        if implementation == "oai" and not component.custom_chart_path:
            raise ValueError(f"{name}: select the nextg-lens OAI charts before enabling CSI")
        if implementation == "ocudu" and component.custom_rendered_files not in (None, _ocudu_rendered_files()):
            raise ValueError(f"{name}: custom_rendered_files already set; CSI needs the nextg-lens OCUDU files")
        directory = output_dir or (OCUDU_OUTPUT_DIR if implementation == "ocudu" else DEFAULT_OUTPUT_DIR)
        if implementation == "ocudu" and not directory.startswith("/mnt/"):
            raise ValueError(f"{name}: the OCUDU gNB chart only mounts the node's /mnt; output_dir must be under it")
        directories[name] = directory
    return directories


def _select_image(name: str, component, image: ContainerImage | None, *, ocudu: bool) -> None:
    """CSI logger image of the component: explicit image, else the CSI image of its default image."""
    if image is not None:
        component.image = image.model_copy(deep=True)
    elif component.image is None:
        return
    elif ocudu:
        if component.image.repository != OCUDU_CSI_REPOSITORY:
            component.image = _ocudu_csi_image(name, component.image)
    elif not component.image.repository.endswith("-csi"):
        repository = CSI_IMAGE_REPOSITORIES.get(component.image.repository)
        if repository is None:
            raise ValueError(f"{name}: no CSI image known for {component.image.repository!r}; pass image=")
        component.image = component.image.model_copy(update={"repository": repository})


def _ocudu_rendered_files() -> list[str]:
    return [str(OCUDU_TEMPLATES / "values.yaml.j2"), str(OCUDU_TEMPLATES / "config.yaml.j2")]


def _ocudu_variants() -> dict:
    """Upstream default OCUDU gNB image (orchestrator DEFAULT_RAN_IMAGES) of each radio variant."""
    defaults = DEFAULT_RAN_IMAGES[RANImplementation.OCUDU]
    return {vendor: defaults[vendor][RANComponentType.GNB] for vendor in OCUDU_CSI_TAGS if vendor in defaults}


def _ocudu_csi_image(name: str, current: ContainerImage) -> ContainerImage:
    for vendor, default in _ocudu_variants().items():
        if current.repository == default.repository:
            return current.model_copy(update={"repository": OCUDU_CSI_REPOSITORY, "image_tag": OCUDU_CSI_TAGS[vendor]})
    raise ValueError(f"{name}: no OCUDU CSI image known for {current.repository!r}; pass image=")


def _check_distinct_files(all_components: dict, selected: dict[str, str]) -> None:
    """Refuse two CSI loggers writing the same hostPath file (same node, same output directory)."""
    files = {}
    for name, component in all_components.items():
        env = component.extra_env_vars or {}
        if name in selected:
            directory = selected[name]
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

    OCUDU: r2labuser/ocudu-gnb-csi without CSI goes back to the upstream default image of its radio variant.

    The logger of these images always starts (oai-csi-logging v3.1), and the orchestrator defaults to
    r2labuser/oai-gnb-aw2s-csi for the AW2S gNB, DU, CU and CU-CP: without enable_csi it would write
    /data/csi inside the container. Every other OAI component with a "-csi" image gets the same image
    without logger: the reverse of CSI_IMAGE_REPOSITORIES, else the repository without its "-csi" suffix
    (r2labuser/oai-gnb-aw2s-csi -> r2labuser/oai-gnb-aw2s), with the same tag.
    """
    without_csi = {csi: plain for plain, csi in CSI_IMAGE_REPOSITORIES.items()}
    ocudu_defaults = {OCUDU_CSI_TAGS[vendor]: default for vendor, default in _ocudu_variants().items()}
    for component in ran.deployment_model.ran.components.values():
        if component.image is None or str((component.extra_env_vars or {}).get("CSI_LOGGING_ENABLED")) == "1":
            continue
        if str(component.implementation) == "ocudu":
            default = ocudu_defaults.get(component.image.image_tag)
            if component.image.repository == OCUDU_CSI_REPOSITORY and default is not None:
                component.image = default.model_copy(deep=True)
            continue
        if str(component.implementation) != "oai":
            continue
        repository = component.image.repository
        if repository.endswith("-csi"):
            plain = without_csi.get(repository, repository.removesuffix("-csi"))
            component.image = component.image.model_copy(update={"repository": plain})
