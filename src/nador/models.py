"""Monitoring configuration and Helm component models."""

import json
import re
from pathlib import Path
from typing import Any, Literal, Self

from metamodel_3gpp.models.base import StrictBaseModel
from ifran.models.deployment import DeploymentModel
from ifran.models.k8s import ContainerImage
from pydantic import Field, model_validator
from nador.observability import ObservabilitySettings
from nador.extended import ExtendedSettings
from nador.one_way import OneWaySettings


DEFAULT_IMAGES = json.loads((Path(__file__).parent / "assets" / "images.json").read_text())


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

    # Require an interface and container for packet capture.
    @model_validator(mode="after")
    def validate_packet_target(self) -> Self:
        if not self.component.strip():
            raise ValueError("component must not be empty")
        if self.role and (not self.interface or not self.container):
            raise ValueError("Monitoring targets require explicit interface and container")
        for interface in (self.interface, self.n3_interface, self.n6_interface):
            if interface is not None and (not interface.strip() or any(c.isspace() for c in interface)):
                raise ValueError("Interfaces must be non-empty names without whitespace")
        return self


class MonitoringImages(StrictBaseModel):
    """Container versions for probes and sniffers injected into network-function pods."""

    probe: str = DEFAULT_IMAGES["probes"]["probe"]
    throughput: str = DEFAULT_IMAGES["probes"]["throughput"]
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
    probe_controller_image: str | None = None
    probe_api_tls_secret: str | None = None
    probe_api_token_secret: str | None = None
    external_throughput_image: str | None = None
    external_path_image: str | None = None
    rtt_placement: Literal["external"] = "external"
    rtt_imsis: list[str] = Field(default_factory=list)
    rtt_slices: list[str] = Field(default_factory=list)
    targets: list[MonitoringTarget] = Field(default_factory=list)
    images: MonitoringImages = Field(default_factory=MonitoringImages)
    components: dict[str, DeployedMonitoringComponent] = Field(default_factory=dict)
    prometheus_scrape_interval: str = "1s"
    prometheus_retention_period: str = "5d"
    operation_timeout_seconds: int = Field(default=300, ge=1)
    prometheus_node_port: int = Field(default=30095, ge=30000, le=32767)
    grafana_node_port: int = Field(default=32005, ge=30000, le=32767)
    loki_node_port: int = Field(default=31000, ge=30000, le=32767)

    @classmethod
    def from_yaml_file(cls, path: str | Path) -> Self:
        """Read and check a full monitoring profile before any cluster work."""
        from nador.validate_monitoring import read_deployment

        return read_deployment(path, cls)

    @property
    def multi_anchor(self) -> bool:
        return any(sum(t.role == role for t in self.targets) > 1 for role in ("gnb", "upf"))

    @property
    def separate_one_way_targets(self) -> bool:
        return self.multi_anchor or not any(t.role == "gnb" for t in self.targets)

    # Check dependencies between enabled features and their targets.
    @model_validator(mode="after")
    def validate_profile(self) -> Self:
        if self.packet_pairing:
            raise ValueError("External RTT does not support packet_pairing; set packet_pairing: false")
        roles = {target.role for target in self.targets}
        if not self.probe_controller_image and (
            ((self.latency or self.rtt_imsis or self.rtt_slices or self.observability.user_plane_paths)
             and self.multi_anchor)
            or (self.one_way.image and self.separate_one_way_targets)
        ):
            raise ValueError("Set probe_controller_image to a build that supports the requested multi-anchor probes")
        external_images = DEFAULT_IMAGES["external_probes"]
        if (self.latency or self.one_way.image or self.observability.user_plane_paths) and not self.probe_controller_image:
            self.probe_controller_image = external_images["controller"]
        if self.probe_controller_image and self.throughput and self.external_throughput_image is None:
            self.external_throughput_image = external_images["throughput"]
        if self.observability.user_plane_paths and self.external_path_image is None:
            self.external_path_image = external_images["user_plane_paths"]
        if self.probe_controller_image:
            for enabled, name, image in (
                (self.throughput, "external_throughput_image", self.external_throughput_image),
                (self.observability.user_plane_paths, "external_path_image", self.external_path_image),
            ):
                if enabled and not image:
                    raise ValueError(f"Controller mode requires {name} for enabled probes")
                if image and (":" not in image.rsplit("/", 1)[-1] or any(c.isspace() for c in image)):
                    raise ValueError(f"{name} must be a tagged image or digest")
        if self.probe_api_tls_secret or self.probe_api_token_secret:
            if not (self.probe_controller_image and self.probe_api_tls_secret and self.probe_api_token_secret):
                raise ValueError("Public probe API requires probe_controller_image and both API secrets")
        if self.probe_controller_image and not (
            ":" in self.probe_controller_image.rsplit("/", 1)[-1]
            and not any(c.isspace() for c in self.probe_controller_image)
        ):
            raise ValueError("probe_controller_image must be a tagged image or digest")
        if self.latency or self.rtt_imsis or self.rtt_slices:
            if not self.probe_controller_image:
                raise ValueError("External RTT requires probe_controller_image")
            if not {"amf", "smf"} <= roles or roles.isdisjoint({"gnb", "upf"}):
                raise ValueError("External RTT requires AMF/SMF and a gNB or UPF target")
        if any(not re.fullmatch(r"[0-9]{5,16}", value) for value in self.rtt_imsis):
            raise ValueError("rtt_imsis must contain quoted IMSIs")
        if any(not re.fullmatch(r"[0-9a-f]{2}:[0-9a-f]{6}", value) for value in self.rtt_slices):
            raise ValueError("rtt_slices must use labels such as 01:ffffff")
        keys = [(t.category, t.component) for t in self.targets]
        if len(keys) != len(set(keys)):
            raise ValueError("Only one monitoring target per component is supported")
        if self.throughput and self.targets and not {"amf", "smf", "upf"} <= roles:
            raise ValueError("Throughput requires AMF, SMF and UPF packet targets")
        if self.one_way.image and not {"amf", "smf", "upf"} <= roles:
            raise ValueError("One-way monitoring requires UPF, AMF and SMF targets for slice mapping")
        if self.one_way.n3.enabled and "gnb" not in roles:
            raise ValueError("N3 one-way monitoring requires at least one gNB and one UPF target")
        observation = self.observability
        if (observation.ngap or observation.mapper_coverage) and "amf" not in roles:
            raise ValueError("NGAP/mapper coverage requires an AMF target")
        if (observation.pfcp or observation.mapper_coverage) and "smf" not in roles:
            raise ValueError("PFCP/mapper coverage requires an SMF target")
        if observation.user_plane_paths and not roles.intersection({"gnb", "upf"}):
            raise ValueError("Path monitoring requires a gnb/upf target")
        for target in self.targets:
            port = {"amf": observation.ngap_port, "smf": observation.pfcp_port,
                    "gnb": observation.path_port, "upf": observation.path_port}.get(target.role)
            if observation.active and target.metrics_port == port:
                raise ValueError(f"{target.component}: observability and NF metric ports conflict")
        if len({self.prometheus_node_port, self.grafana_node_port, self.loki_node_port}) != 3:
            raise ValueError("Monitoring NodePorts must be distinct")
        return self


class MonitoringModel(DeploymentModel):
    """Extension-owned deployment snapshot; the upstream model stays unchanged."""

    monitoring: MonitoringDeployment = Field(default_factory=MonitoringDeployment)
