"""Monitoring configuration and Helm component models."""

from typing import Any, Literal, Self

from metamodel.models.base import StrictBaseModel
from orchestrator.models.deployment import DeploymentModel
from orchestrator.models.k8s import ContainerImage
from pydantic import Field, model_validator


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

    # Require an interface and container for packet capture.
    @model_validator(mode="after")
    def validate_packet_target(self) -> Self:
        if not self.component.strip():
            raise ValueError("component must not be empty")
        if self.role and (not self.interface or not self.container):
            raise ValueError("Monitoring targets require explicit interface and container")
        return self


class MonitoringImages(StrictBaseModel):
    """Container versions for probes and sniffers injected into network-function pods."""

    probe: str = "r2labuser/ebpf-latency-probe:2026e2eLa<YOUR_PASSWORD>Debug"
    throughput: str = "r2labuser/ebpf-throughput-probe:2026w44-mapping"
    amf_sniffer: str = "r2labuser/amf-sniffer:2026w40-tmsi-correlation"
    smf_sniffer: str = "r2labuser/smf-sniffer:2026w10"


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
    targets: list[MonitoringTarget] = Field(default_factory=list)
    images: MonitoringImages = Field(default_factory=MonitoringImages)
    components: dict[str, DeployedMonitoringComponent] = Field(default_factory=dict)
    prometheus_scrape_interval: str = "1s"
    prometheus_retention_period: str = "5d"
    operation_timeout_seconds: int = Field(default=300, ge=1)
    prometheus_node_port: int = Field(default=30095, ge=30000, le=32767)
    grafana_node_port: int = Field(default=32005, ge=30000, le=32767)
    loki_node_port: int = Field(default=31000, ge=30000, le=32767)

    # Check dependencies between enabled features and their targets.
    @model_validator(mode="after")
    def validate_profile(self) -> Self:
        if self.packet_pairing and not self.latency:
            raise ValueError("packet_pairing requires latency")
        keys = [(t.category, t.component) for t in self.targets]
        if len(keys) != len(set(keys)):
            raise ValueError("Only one monitoring target per component is supported")
        if self.latency or (self.throughput and self.targets):
            roles = {t.role for t in self.targets}
            if not {"amf", "smf"} <= roles or not roles & {"gnb", "upf"}:
                raise ValueError("Packet monitoring requires AMF, SMF and at least one gnb/upf packet target")
            if self.packet_pairing and not {"gnb", "upf"} <= roles:
                raise ValueError("Packet pairing requires both gnb and upf targets")
        if self.throughput and self.targets and "upf" not in {t.role for t in self.targets}:
            raise ValueError("Throughput requires a UPF packet target")
        if len({self.prometheus_node_port, self.grafana_node_port, self.loki_node_port}) != 3:
            raise ValueError("Monitoring NodePorts must be distinct")
        return self


class MonitoringModel(DeploymentModel):
    """Extension-owned deployment snapshot; the upstream model stays unchanged."""

    monitoring: MonitoringDeployment = Field(default_factory=MonitoringDeployment)
