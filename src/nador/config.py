"""The monitoring profile: its schema, its validation rules and how it is read from YAML.

A profile (``monitoring.yaml``) becomes a ``MonitoringDeployment``. Sections of the
profile have their own settings models below (``observability``, ``extended``,
``one_way``); ``read_deployment`` is the strict YAML entry point.
"""

import json
import re
from ipaddress import IPv4Address
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from ifran.models.deployment import DeploymentModel
from ifran.models.k8s import ContainerImage
from ifran.utils.utils import deep_merge
from metamodel_3gpp.models.base import StrictBaseModel
from pydantic import AfterValidator, Field, ValidationError, field_validator, model_validator

from nador.probes import PROBES, enabled_probes

# --- Image references ----------------------------------------------------------------------------------------------


def is_pinned(reference: str) -> bool:
    """Whether a reference names a tag or digest (``repo:tag``, ``repo@sha256:...``)."""
    return ":" in reference.rsplit("/", 1)[-1] and not any(c.isspace() for c in reference)


def _require_pinned(reference: str) -> str:
    if not is_pinned(reference):
        raise ValueError("must be a tagged image (repo:tag) or digest (repo@sha256:...)")
    return reference


TaggedImage = Annotated[str, AfterValidator(_require_pinned)]
"""An image reference that never falls back to an implicit ``latest`` tag."""


def split_image(reference: str) -> tuple[str, str]:
    """Split ``repo:tag[@digest]`` into repository and tag, keeping any digest with the tag."""
    name, at, digest = reference.partition("@")
    repository, colon, tag = name.rpartition(":")
    if not colon or "/" in tag:
        repository, tag = name, ""
    return repository, tag + at + digest


# --- observability: protocol, path and infrastructure collectors ---------------------------------------------------


class ObservabilitySettings(StrictBaseModel):
    """Opt-in collectors with optional image overrides."""

    ngap: bool = False
    pfcp: bool = False
    mapper_coverage: bool = False
    user_plane_paths: bool = False
    node_metrics: bool = False
    amf_image: str | None = None
    smf_image: str | None = None
    mapper_image: str | None = None
    ngap_port: int = Field(default=9102, ge=1, le=65535)
    pfcp_port: int = Field(default=9104, ge=1, le=65535)
    path_port: int = Field(default=9403, ge=1, le=65535)
    pending_timeout_seconds: float = Field(default=30, gt=0, le=3600)
    path_interval_seconds: float = Field(default=5, ge=0.1, le=3600)

    @property
    def active(self) -> bool:
        """Whether any collector that needs the session stack is enabled."""
        return self.ngap or self.pfcp or self.mapper_coverage or self.user_plane_paths

    @model_validator(mode="after")
    def require_collector_images(self) -> Self:
        """Reject blank image overrides and exporter port clashes."""
        for field in ("amf_image", "smf_image", "mapper_image"):
            image = getattr(self, field)
            if image is not None and not image.strip():
                raise ValueError(f"{field} must be a non-empty image or null to inherit the default")
        if self.path_port in range(9100, 9200) or self.path_port in range(9300, 9400):
            raise ValueError("path_port overlaps the latency/throughput exporter ranges; use 9403 or another range")
        return self


# --- extended: dedicated research probes ---------------------------------------------------------------------------


class QualityQuery(StrictBaseModel):
    """Use an event timestamp, not a last scrape, to assess observation freshness."""

    name: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,63}$")
    query: str = Field(min_length=1, max_length=4096)
    kind: Literal["event_timestamp", "availability"] = "availability"
    max_age_seconds: float = Field(default=30, gt=0, le=86400)


class ExtendedSettings(StrictBaseModel):
    """Experimental collectors with no inferred deployment or image selection."""

    scheduler_wait: bool = False
    kernel_drops: bool = False
    network_queues: bool = False
    socket_counters: bool = False
    split_signaling: bool = False
    quality: bool = False
    attributed_drops: bool = False
    tcp_flows: bool = False
    packet_identity_secret: str | None = Field(
        default=None, pattern=r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$", max_length=253
    )
    packet_run_id: str = Field(default="experiment", min_length=1, max_length=128)
    packet_state_limit: int = Field(default=4096, ge=16, le=16384)
    packet_ttl_seconds: float = Field(default=30, ge=1, le=300)
    quality_queries: list[QualityQuery] = Field(default_factory=list, max_length=64)
    probe_images: dict[str, TaggedImage] = Field(default_factory=dict)
    probe_targets: dict[str, list[str]] = Field(default_factory=dict)
    interval_seconds: float = Field(default=5, ge=1, le=300)
    timeout_seconds: float = Field(default=2, ge=0.1, le=10)
    capture_limit: int = Field(default=8, ge=1, le=32)

    @model_validator(mode="after")
    def validate_settings(self):
        """Require pinned images and consistent settings for enabled probes."""
        unknown = self.probe_images.keys() - PROBES.keys()
        if unknown:
            raise ValueError(f"Unknown probe images: {sorted(unknown)}")
        missing = set(enabled_probes(self)) - self.probe_images.keys()
        if missing:
            raise ValueError(f"extended.probe_images requires an image for: {', '.join(sorted(missing))}")
        if self.probe_targets.keys() - self.probe_images.keys() or any(
            not items for items in self.probe_targets.values()
        ):
            raise ValueError("probe_targets requires a corresponding probe image and a nonempty target list")
        names = [query.name for query in self.quality_queries]
        if len(set(names)) != len(names):
            raise ValueError("Extended quality query names must be unique")
        if self.timeout_seconds > self.interval_seconds:
            raise ValueError("Probe timeout must not exceed the polling interval")
        if self.tcp_flows and not self.packet_identity_secret:
            raise ValueError("TCP flow evidence requires an existing Kubernetes Secret with key 'key'")
        return self


# --- one_way: controller-managed one-way latency -------------------------------------------------------------------


class N3Settings(StrictBaseModel):
    """One-way latency between gNB and UPF on N3."""

    enabled: bool = False
    log_samples: bool = False


class UpfTransitSettings(StrictBaseModel):
    """One-way transit latency through the UPF towards a test peer."""

    enabled: bool = False
    peer: IPv4Address | None = None


class OneWaySettings(StrictBaseModel):
    """Controller-managed one-way probes and their shared settings."""

    image: TaggedImage | None = None
    sample: int = Field(default=16, ge=1, le=0xFFFFFFFF)
    test_port: int = Field(default=0, ge=0, le=65535)
    n3: N3Settings = Field(default_factory=N3Settings)
    upf_transit: UpfTransitSettings = Field(default_factory=UpfTransitSettings)

    @model_validator(mode="after")
    def check_requirements(self) -> Self:
        """Enabled probes need an image; UPF transit needs a peer and port."""
        if (self.n3.enabled or self.upf_transit.enabled) and not self.image:
            raise ValueError("Enabled one-way probes require one_way.image")
        if self.upf_transit.enabled and (not self.upf_transit.peer or not self.test_port):
            raise ValueError("UPF transit requires upf_transit.peer and test_port")
        return self


# --- The profile ---------------------------------------------------------------------------------------------------

DEFAULT_IMAGES = json.loads((Path(__file__).parent / "assets" / "images.json").read_text())
DURATION = r"^([0-9]+(ms|s|m|h|d|w|y))+$"  # Prometheus duration, e.g. 15s, 5d or 1h30m


def component_image_defaults(name: str, controller_image: str | None = None) -> dict | None:
    """Derive controller chart defaults from the same image used for probes and cleanup."""
    if name == "monitoring-controller":
        reference = controller_image or DEFAULT_IMAGES["external_probes"]["controller"]
        repository, tag = split_image(reference)
        return {"repository": repository, "image_tag": tag, "image_pull_policy": "IfNotPresent"}
    return DEFAULT_IMAGES["components"].get(name)


class DeployedMonitoringComponent(StrictBaseModel):
    """Helm release with optional monitoring configuration overrides."""

    release_name: str | None = None
    chart_ref: str | None = None
    chart_version: str | None = None
    chart_values: dict[str, Any] = Field(default_factory=dict)
    pod_selector: list[str] = Field(default_factory=list)  # empty for charts without workloads
    advanced_config: dict[str, Any] = Field(default_factory=dict)
    namespace: str = "monitoring"
    image: ContainerImage | None = None
    node_name: str | None = None
    node_selector: dict[str, str] | None = None
    tolerations: list[dict[str, Any]] = Field(default_factory=list)
    is_installed: bool = False


class MonitoringTarget(StrictBaseModel):
    """Bind monitoring to a deployment component; e.g. AMF, SMF, gNB, UPF, etc."""

    category: Literal["core", "ran", "ue"]
    component: str
    role: Literal["amf", "smf", "gnb", "upf"] | None = None
    interface: str | None = None
    container: str | None = None
    selector: dict[str, str] = Field(default_factory=dict)
    metrics_port: int | None = Field(default=None, ge=1, le=65535)
    metrics_path: str = "/metrics"
    latency_mode: Literal["CORE", "RAN", "BOTH", "E2E"] = "BOTH"
    n3_interface: str | None = None
    n6_interface: str | None = None
    n3_network_names: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_packet_target(self) -> Self:
        """Require a container and interface for packet capture targets."""
        if not self.component.strip():
            raise ValueError("component must not be empty")
        if self.role and (not self.interface or not self.container):
            raise ValueError("Monitoring targets require explicit interface and container")
        for interface in (self.interface, self.n3_interface, self.n6_interface):
            if interface is not None and (not interface.strip() or any(c.isspace() for c in interface)):
                raise ValueError("Interfaces must be non-empty names without whitespace")
        return self


class ResolvedTarget(MonitoringTarget):
    """A monitoring target located in the deployed network: its namespace and pod selector are known."""

    namespace: str
    implementation: str
    label_selectors: list[str]

    def values(self) -> dict[str, Any]:
        """The target as plain data for Helm values and Ansible inputs."""
        return self.model_dump(mode="json")


class MonitoringImages(StrictBaseModel):
    """Container versions for the RTT probe and session sniffers."""

    probe: str = DEFAULT_IMAGES["probes"]["probe"]
    amf_sniffer: str = DEFAULT_IMAGES["probes"]["amf_sniffer"]
    smf_sniffer: str = DEFAULT_IMAGES["probes"]["smf_sniffer"]


class MonitoringDeployment(StrictBaseModel):
    """Monitoring configuration and component deployment information."""

    namespace: str = Field(default="monitoring", pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63)
    node_name: str | None = None  # actual Kubernetes node name for scheduling
    kubeconfig: str = "/etc/kubernetes/admin.conf"  # on the Kubernetes master
    loki: bool = True
    latency: bool = False
    throughput: bool = True
    packet_pairing: bool = False
    core_kpis: bool = False
    ran_kpis: bool = False
    ocudu_metrics_port: int = Field(default=8001, ge=1, le=65535)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    extended: ExtendedSettings = Field(default_factory=ExtendedSettings)
    one_way: OneWaySettings = Field(default_factory=OneWaySettings)
    probe_controller_image: TaggedImage | None = None
    probe_api_tls_secret: str | None = None
    probe_api_token_secret: str | None = None
    external_throughput_image: TaggedImage | None = None
    external_path_image: TaggedImage | None = None
    rtt_imsis: list[str] = Field(default_factory=list)
    rtt_slices: list[str] = Field(default_factory=list)
    targets: list[MonitoringTarget] = Field(default_factory=list)
    images: MonitoringImages = Field(default_factory=MonitoringImages)
    components: dict[str, DeployedMonitoringComponent] = Field(default_factory=dict)
    prometheus_scrape_interval: str = Field(default="1s", pattern=DURATION)
    prometheus_retention_period: str = Field(default="5d", pattern=DURATION)
    operation_timeout_seconds: int = Field(default=300, ge=1)
    prometheus_node_port: int = Field(default=30095, ge=30000, le=32767)
    grafana_node_port: int = Field(default=32005, ge=30000, le=32767)
    loki_node_port: int = Field(default=31000, ge=30000, le=32767)

    @classmethod
    def from_yaml_file(cls, path: str | Path) -> Self:
        """Read and check a full monitoring profile before any cluster work."""
        return read_deployment(path, cls)

    @property
    def roles(self) -> set[str | None]:
        """Packet-capture roles present among the targets."""
        return {target.role for target in self.targets}

    @property
    def multi_anchor(self) -> bool:
        """Whether more than one gNB or more than one UPF is monitored."""
        return any(sum(t.role == role for t in self.targets) > 1 for role in ("gnb", "upf"))

    @property
    def separate_one_way_targets(self) -> bool:
        """Whether one-way probes need explicit per-anchor targets."""
        return self.multi_anchor or "gnb" not in self.roles

    @property
    def needs_session_stack(self) -> bool:
        """Whether to deploy session services and start the AMF/SMF sniffers."""
        return bool(self.probe_controller_image or self.observability.active)

    @field_validator("rtt_imsis")
    @classmethod
    def _check_imsis(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[0-9]{5,16}", value) for value in values):
            raise ValueError("rtt_imsis must contain quoted IMSIs")
        return values

    @field_validator("rtt_slices")
    @classmethod
    def _check_slices(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[0-9a-f]{2}:[0-9a-f]{6}", value) for value in values):
            raise ValueError("rtt_slices must use labels such as 01:ffffff")
        return values

    @model_validator(mode="after")
    def validate_profile(self) -> Self:
        """Fill default probe images, then check that the enabled features fit the targets."""
        self._apply_default_images()
        self._check_targets()
        self._check_probes()
        self._check_observability()
        self._check_ports()
        return self

    def _apply_default_images(self) -> None:
        multi_target_paths = self.multi_anchor and self.observability.user_plane_paths
        separate_one_way = self.one_way.image and self.separate_one_way_targets
        if not self.probe_controller_image and (multi_target_paths or separate_one_way):
            raise ValueError("Set probe_controller_image to a build that supports the requested multi-anchor probes")
        defaults = DEFAULT_IMAGES["external_probes"]
        wants_controller = self.latency or self.throughput or self.one_way.image or self.observability.user_plane_paths
        if wants_controller and not self.probe_controller_image:
            self.probe_controller_image = defaults["controller"]
        if self.throughput and self.external_throughput_image is None:
            self.external_throughput_image = defaults["throughput"]
        if self.observability.user_plane_paths and self.external_path_image is None:
            self.external_path_image = defaults["user_plane_paths"]

    def _check_targets(self) -> None:
        keys = [(t.category, t.component) for t in self.targets]
        if len(keys) != len(set(keys)):
            raise ValueError("Only one monitoring target per component is supported")

    def _check_probes(self) -> None:
        roles = self.roles
        if self.packet_pairing and (not self.latency or not {"gnb", "upf"} <= roles):
            raise ValueError("Packet pairing requires latency and both gNB and UPF targets")
        api_secrets = (self.probe_api_tls_secret, self.probe_api_token_secret)
        if any(api_secrets) and not (self.probe_controller_image and all(api_secrets)):
            raise ValueError("Public probe API requires probe_controller_image and both API secrets")
        if self.latency or self.rtt_imsis or self.rtt_slices:
            if not self.probe_controller_image:
                raise ValueError("External RTT requires probe_controller_image")
            if not {"amf", "smf"} <= roles or roles.isdisjoint({"gnb", "upf"}):
                raise ValueError("External RTT requires AMF/SMF and a gNB or UPF target")
        # An empty target list is allowed here so defaults can be built; plan() requires targets.
        if self.throughput and self.targets and not {"amf", "smf", "upf"} <= roles:
            raise ValueError("Throughput requires AMF, SMF and UPF packet targets")
        if self.one_way.image and not {"amf", "smf", "upf"} <= roles:
            raise ValueError("One-way monitoring requires UPF, AMF and SMF targets for slice mapping")
        if self.one_way.n3.enabled and "gnb" not in roles:
            raise ValueError("N3 one-way monitoring requires at least one gNB and one UPF target")

    def _check_observability(self) -> None:
        observation, roles = self.observability, self.roles
        if (observation.ngap or observation.mapper_coverage) and "amf" not in roles:
            raise ValueError("NGAP/mapper coverage requires an AMF target")
        if (observation.pfcp or observation.mapper_coverage) and "smf" not in roles:
            raise ValueError("PFCP/mapper coverage requires an SMF target")
        if observation.user_plane_paths and not roles.intersection({"gnb", "upf"}):
            raise ValueError("Path monitoring requires a gnb/upf target")

    def _check_ports(self) -> None:
        observation = self.observability
        if observation.active:
            ports = {
                "amf": observation.ngap_port,
                "smf": observation.pfcp_port,
                "gnb": observation.path_port,
                "upf": observation.path_port,
            }
            for target in self.targets:
                if target.role and target.metrics_port == ports[target.role]:
                    raise ValueError(f"{target.component}: observability and NF metric ports conflict")
        node_ports = (self.prometheus_node_port, self.grafana_node_port, self.loki_node_port)
        if len(set(node_ports)) != len(node_ports):
            raise ValueError("Monitoring NodePorts must be distinct")


class MonitoringModel(DeploymentModel):
    """Extension-owned deployment snapshot; the upstream model stays unchanged."""

    monitoring: MonitoringDeployment = Field(default_factory=MonitoringDeployment)


# --- Reading profiles from YAML ------------------------------------------------------------------------------------


class ProfileLoader(yaml.SafeLoader):
    """Reject duplicate options instead of silently choosing the last value."""

    def construct_mapping(self, node, deep=False):
        """Build a mapping, rejecting duplicate keys."""
        mapping = super().construct_mapping(node, deep=deep)
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if len(keys) != len(mapping):
            raise yaml.constructor.ConstructorError(
                None,
                None,
                "duplicate monitoring option",
                node.start_mark,
            )
        return mapping


ProfileLoader.yaml_implicit_resolvers = {
    ch: [(tag, pattern) for tag, pattern in entries if tag != "tag:yaml.org,2002:bool"]
    for ch, entries in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
ProfileLoader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile("^(true|false)$"), list("tf"))


def switches(defaults=None, prefix=()):
    """Yield explicit switch paths, including switches introduced by new model fields."""
    if defaults is None:
        defaults = MonitoringDeployment().model_dump(mode="json", exclude={"components", "images", "targets"})
    for key, value in defaults.items():
        path = (*prefix, key)
        if type(value) is bool:
            yield path
        elif isinstance(value, dict):
            yield from switches(value, path)


def load_profile(path):
    """Read a profile and fill partial component image overrides."""
    data = yaml.load(path.read_text(), Loader=ProfileLoader)  # noqa: S506 - ProfileLoader is a SafeLoader
    if not isinstance(data, dict):
        raise ValueError("expected a YAML mapping")
    components = data.get("components")
    if isinstance(components, dict):
        for name, override in components.items():
            image = override.get("image") if isinstance(override, dict) else None
            if isinstance(image, dict) and (default_image := component_image_defaults(name)):
                override["image"] = deep_merge(default_image.copy(), image)
    return data


def validate_deployment[M: MonitoringDeployment](
    data: dict,
    model: type[M] = MonitoringDeployment,
    *,
    source: str = "Monitoring configuration",
    explicit: bool = False,
) -> M:
    """Validate deployment values and report field errors without displaying input values."""
    errors = []
    if explicit:
        for keys in switches():
            value = data
            for key in keys:
                value = value.get(key) if isinstance(value, dict) else None
            if type(value) is not bool:
                errors.append(f"{'.'.join(keys)}: write an explicit true or false")
    try:
        config = model.model_validate(data)
    except ValidationError as exc:
        errors.extend(f"{'.'.join(map(str, error['loc'])) or 'profile'}: {error['msg']}" for error in exc.errors())
        raise ValueError(_report(source, errors)) from None
    if errors:
        raise ValueError(_report(source, errors))
    return config


def _report(source: str, errors: list[str]) -> str:
    return f"{source}:\n  - " + "\n  - ".join(errors)


def read_deployment[M: MonitoringDeployment](path: str | Path, model: type[M] = MonitoringDeployment) -> M:
    """Load a full monitoring profile before planning or deploying it."""
    path = Path(path)
    try:
        data = load_profile(path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ValueError(f"Invalid monitoring profile {path}: {exc}") from exc
    return validate_deployment(data, model, source=f"Invalid monitoring profile {path}", explicit=True)
