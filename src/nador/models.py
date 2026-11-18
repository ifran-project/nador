"""Monitoring configuration and Helm component models."""

import json
import re
from pathlib import Path
from typing import Any, Literal, Self

from ifran.models.deployment import DeploymentModel
from ifran.models.k8s import ContainerImage
from metamodel_3gpp.models.base import StrictBaseModel
from pydantic import Field, field_validator, model_validator

from nador.extended import ExtendedSettings
from nador.images import TaggedImage, split_image
from nador.observability import ObservabilitySettings
from nador.one_way import OneWaySettings

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
        from nador.validate_monitoring import read_deployment  # noqa: PLC0415 - avoids a circular import

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
