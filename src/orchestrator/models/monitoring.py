"""Monitoring configuration independent of implementation-specific namespaces."""

from typing import Any, Literal, Self

from metamodel.models.base import StrictBaseModel
from pydantic import Field, model_validator

from orchestrator.models.common import Deployed5GComponent


class DeployedMonitoringComponent(Deployed5GComponent):
    """Same deployment base as core and RAN components."""

    implementation: str = "monitoring"
    type: str = "monitoring"
    release_name: str | None = None
    chart_ref: str | None = None
    chart_version: str | None = None
    pod_selector: list[str] = Field(default_factory=list)
    tolerations: list[dict[str, Any]] | None = None
    advanced_config: dict[str, Any] = Field(default_factory=dict)
    chart_values: dict[str, Any] = Field(default_factory=dict)


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

    @model_validator(mode="after")
    def validate_packet_target(self) -> Self:
        if not self.component.strip():
            raise ValueError("component must not be empty")
        if self.role and (not self.interface or not self.container):
            raise ValueError("Monitoring targets require explicit interface and container")
        if self.interface and any(c.isspace() for c in self.interface):
            raise ValueError("interface must not contain whitespace")
        if not self.metrics_path.startswith("/"):
            raise ValueError("metrics_path must start with /")
        return self


class MonitoringImages(StrictBaseModel):
    """Container versions for probes and sniffers injected into network-function pods."""

    probe: str = "r2labuser/ebpf-latency-probe:2026e2eLa<YOUR_PASSWORD>Debug"
    amf_sniffer: str = "r2labuser/amf-sniffer:2026w40-tmsi-correlation"
    smf_sniffer: str = "r2labuser/smf-sniffer:2026w10"


class MonitoringState(StrictBaseModel):
    """Runtime status and resources recorded for inspection and removal."""

    status: Literal["not_deployed", "deploying", "deployed", "failed", "removing"] = "not_deployed"
    last_error: str | None = None
    installed_namespace: str | None = None
    installed_control_node: str | None = None
    installed_kubeconfig: str | None = None
    installed_latency: bool = False
    installed_targets: list[dict] = Field(default_factory=list)


class MonitoringDeployment(StrictBaseModel):
    """Monitoring configuration with separate container images and runtime state."""

    namespace: str = Field(default="monitoring", pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63)
    node_name: str | None = None  # actual Kubernetes node name for scheduling
    kubeconfig: str = "/etc/kubernetes/admin.conf"  # on the Kubernetes master
    loki: bool = True
    latency: bool = False
    packet_pairing: bool = False
    core_kpis: bool = False
    ran_kpis: bool = False
    targets: list[MonitoringTarget] = Field(default_factory=list)
    images: MonitoringImages = Field(default_factory=MonitoringImages)
    components: dict[str, DeployedMonitoringComponent] = Field(default_factory=dict)
    state: MonitoringState = Field(default_factory=MonitoringState)
    prometheus_scrape_interval: str = "1s"
    prometheus_retention_period: str = "5d"
    operation_timeout_seconds: int = Field(default=300, ge=1)
    prometheus_node_port: int = Field(default=30095, ge=30000, le=32767)
    grafana_node_port: int = Field(default=32005, ge=30000, le=32767)
    loki_node_port: int = Field(default=31000, ge=30000, le=32767)

    @model_validator(mode="after")
    def validate_profile(self) -> Self:
        if self.packet_pairing and not self.latency:
            raise ValueError("packet_pairing requires latency")
        keys = [(t.category, t.component) for t in self.targets]
        if len(keys) != len(set(keys)):
            raise ValueError("Only one monitoring target per component is supported")
        if self.latency:
            roles = {t.role for t in self.targets}
            if not {"amf", "smf"} <= roles or not roles & {"gnb", "upf"}:
                raise ValueError("Latency requires AMF, SMF and at least one gnb/upf packet target")
            if self.packet_pairing and not {"gnb", "upf"} <= roles:
                raise ValueError("Packet pairing requires both gnb and upf targets")
        if len({self.prometheus_node_port, self.grafana_node_port, self.loki_node_port}) != 3:
            raise ValueError("Monitoring NodePorts must be distinct")
        return self
