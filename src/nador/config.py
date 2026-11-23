"""The monitoring profile: which metrics to collect, where, and how it is read from YAML.

A profile (``monitoring.yaml``) becomes a ``MonitoringDeployment``. Every metric is a key
under its group in ``metrics:`` (``ran``, ``core``, ``e2e``, ``host``): ``false`` leaves it
out, ``true`` turns on a metric without options, and a mapping with every option turns on
one that has them. Probes the controller can start later take ``on_demand: true``.
Container images default to ``assets/images.json``; ``images:`` overrides them.
"""

import json
import re
from ipaddress import IPv4Address
from pathlib import Path
from typing import Annotated, Any, Literal, Self, get_args

import yaml
from ifran.models.deployment import DeploymentModel
from ifran.models.k8s import ContainerImage
from ifran.utils.utils import deep_merge
from metamodel_3gpp.models.base import StrictBaseModel
from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    SerializerFunctionWrapHandler,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)

from nador.probes import HOST_PROBES, image_key, kind_of

DEFAULT_IMAGES: dict[str, str] = json.loads((Path(__file__).parent / "assets" / "images.json").read_text())["images"]
DURATION = r"^([0-9]+(ms|s|m|h|d|w|y))+$"  # Prometheus duration, e.g. 15s, 5d or 1h30m
# Helm releases whose image comes from images.json, by image key.
COMPONENT_IMAGES = {
    "monitoring-controller": "controller",
    "ue-mapper-api": "ue_mapper",
    "kpi-calculator-oai": "kpi_calculator_oai",
    "rnti-mapper": "rnti_mapper",
}


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


# --- Metrics -------------------------------------------------------------------------------------------------------


class Metric(StrictBaseModel):
    """A metric that is on. Subclasses add its options."""


class OnDemand(Metric):
    """A probe the controller manages: ``on_demand`` deploys it stopped, ready to start later."""

    on_demand: bool = False


class Rtt(OnDemand):
    """TCP round-trip time per UE and slice, measured by eBPF probes on the N3 side of gNBs and UPFs.

    ``at`` chooses where it is measured; ``packet_pairing`` matches the same packet at both to split
    the latency into segments, so it needs ``at: [gnb, upf]``. Empty ``imsis`` and ``slices`` mean all.
    """

    at: list[Literal["gnb", "upf"]] = Field(default_factory=lambda: ["gnb", "upf"], min_length=1)
    packet_pairing: bool = False
    imsis: list[str] = Field(default_factory=list)
    slices: list[str] = Field(default_factory=list)

    @field_validator("at")
    @classmethod
    def _check_at(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("rtt at lists gnb and upf at most once each")
        return values

    @model_validator(mode="after")
    def _pairing_needs_both(self) -> Self:
        if self.packet_pairing and set(self.at) != {"gnb", "upf"}:
            raise ValueError("rtt packet_pairing needs at: [gnb, upf]")
        return self

    @field_validator("imsis")
    @classmethod
    def _check_imsis(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[0-9]{5,16}", value) for value in values):
            raise ValueError("rtt imsis must contain quoted IMSIs")
        return values

    @field_validator("slices")
    @classmethod
    def _check_slices(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"[0-9a-f]{2}:[0-9a-f]{6}", value) for value in values):
            raise ValueError("rtt slices must use labels such as 01:ffffff")
        return values


class N3OneWay(OnDemand):
    """One-way latency between gNB and UPF on N3."""

    log_samples: bool = False


class UpfTransit(OnDemand):
    """One-way transit latency through the UPF towards a test peer."""

    peer: IPv4Address
    test_port: int = Field(ge=1, le=65535)


class UserPlanePaths(OnDemand):
    """Per-path packet counters on gNBs and UPFs."""

    port: int = Field(default=9403, ge=1, le=65535)
    interval_seconds: float = Field(default=5, ge=0.1, le=3600)

    @field_validator("port")
    @classmethod
    def _check_port(cls, port: int) -> int:
        if port in range(9100, 9200) or port in range(9300, 9400):
            raise ValueError("user_plane_paths port overlaps the latency/throughput exporter ranges")
        return port


class Ngap(Metric):
    """NGAP session-setup metrics exported by the AMF sniffer."""

    port: int = Field(default=9102, ge=1, le=65535)


class Pfcp(Metric):
    """PFCP session-establishment metrics exported by the SMF sniffer."""

    port: int = Field(default=9104, ge=1, le=65535)


class RanKpis(Metric):
    """RAN KPIs: OAI's parser, or OCUDU's native JSON metrics on ``ocudu_port``."""

    ocudu_port: int = Field(default=8001, ge=1, le=65535)


# SRS periods OCUDU accepts (ms); OAI chooses its own period from the TDD pattern and the number of UEs.
OCUDU_SRS_PERIODS_MS = (1, 2, 2.5, 4, 5, 8, 10, 16, 20, 32, 40, 64, 80, 160, 320, 640, 1280, 2560)
INDICES = r"^(all|[0-9]+(,[0-9]+)*)$"


class Csi(Metric):
    """Channel state information logged by OAI ``gnb``/``du`` and OCUDU ``gnb``/``du`` (``configure_csi``).

    ``components`` limits it to some of them (empty: every supported one). ``srs_period_ms`` is OCUDU's SRS period.
    ``granularity`` is one value per resource block (``rb``) or per subcarrier, keeping one in
    ``subcarrier_sampling``. ``antennas`` and ``ports`` select receive antennas and SRS ports (``all`` or ``0,1``).
    ``output_dir`` is where the logger writes on its node (default ``/data/csi`` for OAI, ``/mnt/csi`` for OCUDU).
    """

    components: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=list)
    srs_period_ms: float = 40
    granularity: Literal["rb", "subcarrier"] = "rb"
    subcarrier_sampling: int = Field(default=1, ge=1, le=12)
    antennas: str = Field(default="all", pattern=INDICES)
    ports: str = Field(default="all", pattern=INDICES)
    output_dir: str | None = Field(default=None, pattern=r"^/[^\s]*[^/\s]$")

    @field_validator("srs_period_ms")
    @classmethod
    def _ocudu_period(cls, period: float) -> float:
        if period not in OCUDU_SRS_PERIODS_MS:
            raise ValueError(f"csi srs_period_ms must be one of {', '.join(f'{p:g}' for p in OCUDU_SRS_PERIODS_MS)}")
        return period

    @model_validator(mode="after")
    def _sampling_needs_subcarriers(self) -> Self:
        if self.subcarrier_sampling != 1 and self.granularity != "subcarrier":
            raise ValueError("csi subcarrier_sampling applies to granularity: subcarrier")
        return self

    def logger_env(self) -> dict[str, str]:
        """The logger's environment variables for these options."""
        env = {"CSI_GRANULARITY": self.granularity, "CSI_ANTENNA_SELECTION": self.antennas}
        env["CSI_PORT_SELECTION"] = self.ports
        if self.granularity == "subcarrier":
            env["CSI_SUBCARRIER_SAMPLING"] = str(self.subcarrier_sampling)
        return env


class HostProbe(OnDemand):
    """A probe that runs on the nodes. ``targets`` limits it to some components (``ran/gnb``)."""

    targets: list[str] = Field(default_factory=list)


class TcpFlows(HostProbe):
    """Per-flow TCP evidence; packet identities are keyed by an existing Kubernetes Secret."""

    identity_secret: str = Field(pattern=r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$", max_length=253)


class QualityQuery(StrictBaseModel):
    """Use an event timestamp, not a last scrape, to assess observation freshness."""

    name: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,63}$")
    query: str = Field(min_length=1, max_length=4096)
    kind: Literal["event_timestamp", "availability"] = "availability"
    max_age_seconds: float = Field(default=30, gt=0, le=86400)


class MeasurementQuality(HostProbe):
    """Freshness and availability checks over Prometheus queries."""

    queries: list[QualityQuery] = Field(default_factory=list, max_length=64)

    @field_validator("queries")
    @classmethod
    def _unique(cls, queries: list[QualityQuery]) -> list[QualityQuery]:
        if len({query.name for query in queries}) != len(queries):
            raise ValueError("measurement_quality query names must be unique")
        return queries


def _on_off(value: Any) -> Any:
    """``true`` turns a metric on with default options, ``false`` or ``null`` leaves it off."""
    if value is True:
        return {}
    return None if value is False else value


Switch = BeforeValidator(_on_off)


# Where each metric measures; profiles write metrics under these groups.
METRIC_GROUPS = {
    "ran": ("ran_kpis", "csi", "split_signaling", "ran_logs"),
    "core": ("ngap", "pfcp", "throughput", "upf_transit", "ue_mapper_coverage", "core_logs"),
    "e2e": ("rtt", "n3_one_way", "user_plane_paths"),
    "host": (
        "node_metrics",
        "host_logs",
        "scheduler_wait",
        "kernel_drops",
        "attributed_drops",
        "network_queues",
        "socket_counters",
        "tcp_flows",
        "measurement_quality",
    ),
}
GROUP_OF = {name: group for group, names in METRIC_GROUPS.items() for name in names}


def profile_key(name: str) -> str:
    """How a metric is written inside its group: ``ran_logs`` is ``logs`` under ``ran``."""
    group = GROUP_OF[name]
    return name.removeprefix(f"{group}_") if name == f"{group}_logs" else name


def metric_path(name: str) -> str:
    """The metric's place in a profile, such as ``metrics.e2e.rtt`` or ``metrics.core.logs``."""
    return f"metrics.{GROUP_OF[name]}.{profile_key(name)}"


class Metrics(StrictBaseModel):
    """Every metric NADOR can collect, each ``None`` when off. Profiles group them as ``METRIC_GROUPS``."""

    # ran
    ran_kpis: Annotated[RanKpis | None, Switch] = None
    csi: Annotated[Csi | None, Switch] = None
    split_signaling: Annotated[HostProbe | None, Switch] = None
    ran_logs: Annotated[Metric | None, Switch] = None  # pods in the RAN's namespaces
    # core
    ngap: Annotated[Ngap | None, Switch] = None
    pfcp: Annotated[Pfcp | None, Switch] = None
    throughput: Annotated[OnDemand | None, Switch] = None
    upf_transit: Annotated[UpfTransit | None, Switch] = None
    ue_mapper_coverage: Annotated[Metric | None, Switch] = None
    core_logs: Annotated[Metric | None, Switch] = None  # pods in the core's namespaces
    # e2e
    rtt: Annotated[Rtt | None, Switch] = None
    n3_one_way: Annotated[N3OneWay | None, Switch] = None
    user_plane_paths: Annotated[UserPlanePaths | None, Switch] = None
    # host
    node_metrics: Annotated[Metric | None, Switch] = None
    host_logs: Annotated[Metric | None, Switch] = None  # NADOR's monitoring pods
    scheduler_wait: Annotated[HostProbe | None, Switch] = None
    kernel_drops: Annotated[HostProbe | None, Switch] = None
    attributed_drops: Annotated[HostProbe | None, Switch] = None
    network_queues: Annotated[HostProbe | None, Switch] = None
    socket_counters: Annotated[HostProbe | None, Switch] = None
    tcp_flows: Annotated[TcpFlows | None, Switch] = None
    measurement_quality: Annotated[MeasurementQuality | None, Switch] = None

    @model_validator(mode="before")
    @classmethod
    def _ungroup(cls, data: Any) -> Any:
        """Read ``{group: {metric: value}}``; each metric must sit in its own group."""
        if not isinstance(data, dict):
            return data
        keys = {(group, profile_key(name)): name for name in GROUP_OF for group in [GROUP_OF[name]]}
        flat = {}
        for group, entries in data.items():
            if group not in METRIC_GROUPS:
                hint = f"; {group} belongs under {metric_path(group)}" if group in GROUP_OF else ""
                raise ValueError(f"metrics.{group}: groups are {', '.join(METRIC_GROUPS)}{hint}")
            if not isinstance(entries, dict):
                raise ValueError(f"metrics.{group}: expected a mapping of metrics")
            for key, value in entries.items():
                if (group, key) in keys:
                    flat[keys[(group, key)]] = value
                    continue
                places = [metric_path(name) for (_, written), name in keys.items() if written == key]
                where = f"; it belongs under {' or '.join(places)}" if places else ": unknown metric"
                raise ValueError(f"metrics.{group}.{key}{where}")
        return flat

    @model_serializer(mode="wrap")
    def _group(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        flat = handler(self)
        return {group: {profile_key(name): flat[name] for name in names} for group, names in METRIC_GROUPS.items()}

    def get(self, name: str) -> Metric | None:
        """The metric's options, or None when it is off."""
        return getattr(self, name)

    def available(self, name: str) -> bool:
        """Deployed, running or on demand."""
        return self.get(name) is not None

    def running(self, name: str) -> bool:
        """Deployed and started."""
        metric = self.get(name)
        return metric is not None and not getattr(metric, "on_demand", False)

    @property
    def log_groups(self) -> list[str]:
        """The groups whose pods' logs are collected: ``ran``, ``core`` and/or ``host``."""
        return [group for group in ("ran", "core", "host") if self.get(f"{group}_logs") is not None]


class Tuning(StrictBaseModel):
    """Advanced knobs shared by several probes; the defaults suit most deployments."""

    probe_interval_seconds: float = Field(default=5, ge=1, le=300)
    probe_timeout_seconds: float = Field(default=2, ge=0.1, le=10)
    probe_capture_limit: int = Field(default=8, ge=1, le=32)
    packet_run_id: str = Field(default="experiment", min_length=1, max_length=128)
    packet_state_limit: int = Field(default=4096, ge=16, le=16384)
    packet_ttl_seconds: float = Field(default=30, ge=1, le=300)
    sniffer_pending_timeout_seconds: float = Field(default=30, gt=0, le=3600)
    one_way_sample: int = Field(default=16, ge=1, le=0xFFFFFFFF)

    @model_validator(mode="after")
    def _timeout_fits_interval(self) -> Self:
        if self.probe_timeout_seconds > self.probe_interval_seconds:
            raise ValueError("probe_timeout_seconds must not exceed probe_interval_seconds")
        return self


# --- Targets and Helm components -----------------------------------------------------------------------------------


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


# --- The profile ---------------------------------------------------------------------------------------------------

CONTROLLER_METRICS = ("rtt", "throughput", "n3_one_way", "upf_transit", "user_plane_paths")
SESSION_METRICS = ("ngap", "pfcp", "ue_mapper_coverage", "user_plane_paths")


class MonitoringDeployment(StrictBaseModel):
    """A monitoring profile: the metrics to collect, the network functions to watch, and where to deploy."""

    namespace: str = Field(default="monitoring", pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63)
    node_name: str | None = None  # actual Kubernetes node name for scheduling
    kubeconfig: str = "/etc/kubernetes/admin.conf"  # on the Kubernetes master
    metrics: Metrics = Field(default_factory=Metrics)
    targets: list[MonitoringTarget] = Field(default_factory=list)
    images: dict[str, TaggedImage] = Field(default_factory=dict)
    tuning: Tuning = Field(default_factory=Tuning)
    components: dict[str, DeployedMonitoringComponent] = Field(default_factory=dict)
    probe_api_tls_secret: str | None = None
    probe_api_token_secret: str | None = None
    prometheus_scrape_interval: str = Field(default="1s", pattern=DURATION)
    prometheus_retention_period: str = Field(default="5d", pattern=DURATION)
    operation_timeout_seconds: int = Field(default=300, ge=1)
    prometheus_node_port: int = Field(default=30095, ge=30000, le=32767)
    grafana_node_port: int = Field(default=32005, ge=30000, le=32767)
    loki_node_port: int = Field(default=31000, ge=30000, le=32767)

    @classmethod
    def from_yaml_file(cls, path: str | Path) -> Self:
        """Read and check a full monitoring profile before any cluster work."""
        path = Path(path)
        try:
            data = yaml.load(path.read_text(), Loader=ProfileLoader)  # noqa: S506 - ProfileLoader is a SafeLoader
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError(f"Invalid monitoring profile {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"Invalid monitoring profile {path}: expected a YAML mapping")
        return validate_deployment(data, cls, source=f"Invalid monitoring profile {path}", explicit=True)

    def image(self, key: str) -> str:
        """The image for a key of images.json, after profile overrides."""
        return self.images.get(key) or DEFAULT_IMAGES[key]

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
    def one_way_probes(self) -> bool:
        """Whether any one-way probe is deployed."""
        return self.metrics.available("n3_one_way") or self.metrics.available("upf_transit")

    @property
    def uses_controller(self) -> bool:
        """Whether the probe controller is deployed."""
        on_demand = any(getattr(self.metrics.get(name), "on_demand", False) for name in Metrics.model_fields)
        return on_demand or any(self.metrics.available(name) for name in CONTROLLER_METRICS)

    @property
    def needs_session_stack(self) -> bool:
        """Whether to deploy session services and start the AMF/SMF sniffers."""
        return self.uses_controller or any(self.metrics.running(name) for name in SESSION_METRICS)

    def host_probe_config(self) -> dict[str, Any]:
        """Settings sent to every dedicated probe, in the shape the probe images read."""
        metrics, tuning = self.metrics, self.tuning
        flags = {flag: metrics.running(metric) for metric, flag in HOST_PROBES.items()}
        targets: dict[str, list[str]] = {}
        for metric in HOST_PROBES:
            probe = metrics.get(metric)
            if isinstance(probe, HostProbe) and probe.targets:
                kind = kind_of(metric)
                targets[kind] = sorted({*targets.get(kind, []), *probe.targets})
        quality = metrics.measurement_quality
        return flags | {
            "packet_identity_secret": metrics.tcp_flows.identity_secret if metrics.tcp_flows else None,
            "packet_run_id": tuning.packet_run_id,
            "packet_state_limit": tuning.packet_state_limit,
            "packet_ttl_seconds": float(tuning.packet_ttl_seconds),
            "quality_queries": [query.model_dump(mode="json") for query in quality.queries] if quality else [],
            "probe_targets": targets,
            "interval_seconds": float(tuning.probe_interval_seconds),
            "timeout_seconds": float(tuning.probe_timeout_seconds),
            "capture_limit": tuning.probe_capture_limit,
        }

    def host_probe_images(self) -> dict[str, str]:
        """Image of every deployed host probe, by probe kind."""
        kinds = {kind_of(metric) for metric in HOST_PROBES if self.metrics.available(metric)}
        return {kind: self.image(image_key(kind)) for kind in sorted(kinds)}

    @field_validator("images")
    @classmethod
    def _known_images(cls, images: dict[str, str]) -> dict[str, str]:
        if unknown := images.keys() - DEFAULT_IMAGES.keys():
            raise ValueError(f"Unknown images {sorted(unknown)}; known: {sorted(DEFAULT_IMAGES)}")
        for key in ("ue_mapper", "kpi_calculator_oai"):
            if key in images and not split_image(images[key])[1].partition("@")[0]:
                raise ValueError(f"images.{key} must use an explicit repository:tag")
        return images

    @model_validator(mode="after")
    def validate_profile(self) -> Self:
        """Check that the enabled metrics fit the targets."""
        self._check_targets()
        self._check_controller()
        self._check_roles()
        self._check_ports()
        return self

    def _check_targets(self) -> None:
        keys = [(t.category, t.component) for t in self.targets]
        if len(keys) != len(set(keys)):
            raise ValueError("Only one monitoring target per component is supported")

    def _check_controller(self) -> None:
        metrics = self.metrics
        multi_anchor_paths = self.multi_anchor and metrics.available("user_plane_paths")
        anchored_one_way = self.one_way_probes and self.separate_one_way_targets
        if "controller" not in self.images and (multi_anchor_paths or anchored_one_way):
            raise ValueError("Set images.controller to a build that supports the requested multi-anchor probes")
        api_secrets = (self.probe_api_tls_secret, self.probe_api_token_secret)
        if any(api_secrets) and not (self.uses_controller and all(api_secrets)):
            raise ValueError("Public probe API requires a controller-managed metric and both API secrets")

    def _check_roles(self) -> None:
        """Each metric needs the network functions it measures among the targets."""
        metrics, roles = self.metrics, self.roles
        if metrics.rtt and metrics.rtt.packet_pairing and metrics.rtt.on_demand:
            raise ValueError("Packet pairing requires rtt to run from the start (on_demand: false)")
        if metrics.rtt and not {"amf", "smf"} <= roles:
            raise ValueError("rtt requires AMF and SMF targets to map sessions to UEs")
        if metrics.rtt and self.targets and not set(metrics.rtt.at) <= roles:
            missing = sorted(set(metrics.rtt.at) - roles)
            raise ValueError(f"rtt measures at {', '.join(missing)}, but no target has that role")
        # An empty target list is allowed here so defaults can be built; plan() requires targets.
        if metrics.throughput and self.targets and not {"amf", "smf", "upf"} <= roles:
            raise ValueError("throughput requires AMF, SMF and UPF packet targets")
        if self.one_way_probes and not {"amf", "smf", "upf"} <= roles:
            raise ValueError("One-way metrics require UPF, AMF and SMF targets for slice mapping")
        if metrics.n3_one_way and "gnb" not in roles:
            raise ValueError("n3_one_way requires at least one gNB and one UPF target")
        if (metrics.ngap or metrics.ue_mapper_coverage) and "amf" not in roles:
            raise ValueError("ngap/ue_mapper_coverage require an AMF target")
        if (metrics.pfcp or metrics.ue_mapper_coverage) and "smf" not in roles:
            raise ValueError("pfcp/ue_mapper_coverage require an SMF target")
        if metrics.user_plane_paths and not roles.intersection({"gnb", "upf"}):
            raise ValueError("user_plane_paths requires a gnb/upf target")

    def _check_ports(self) -> None:
        metrics = self.metrics
        ports = {
            "amf": metrics.ngap.port if metrics.ngap else None,
            "smf": metrics.pfcp.port if metrics.pfcp else None,
            "gnb": metrics.user_plane_paths.port if metrics.user_plane_paths else None,
            "upf": metrics.user_plane_paths.port if metrics.user_plane_paths else None,
        }
        for target in self.targets:
            if target.role and target.metrics_port is not None and target.metrics_port == ports[target.role]:
                raise ValueError(f"{target.component}: a NADOR metric port conflicts with the NF metrics port")
        node_ports = (self.prometheus_node_port, self.grafana_node_port, self.loki_node_port)
        if len(set(node_ports)) != len(node_ports):
            raise ValueError("Monitoring NodePorts must be distinct")


def component_image_defaults(name: str, images: dict[str, str] | None = None) -> dict | None:
    """ContainerImage fields for a Helm release whose image comes from images.json or images: overrides."""
    key = COMPONENT_IMAGES.get(name)
    if key is None:
        return None
    repository, tag = split_image((images or {}).get(key) or DEFAULT_IMAGES[key])
    return {"repository": repository, "image_tag": tag, "image_pull_policy": "IfNotPresent"}


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
            raise yaml.constructor.ConstructorError(None, None, "duplicate monitoring option", node.start_mark)
        return mapping


# Only the literal words true/false are booleans: "yes", "on" or "no" stay strings and fail validation.
ProfileLoader.yaml_implicit_resolvers = {
    ch: [(tag, pattern) for tag, pattern in entries if tag != "tag:yaml.org,2002:bool"]
    for ch, entries in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
ProfileLoader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile("^(true|false)$"), list("tf"))


def _complete_component_images(data: dict) -> dict:
    """Fill the repository and tag of ``components.<name>.image`` overrides that only set other fields."""
    components = data.get("components")
    if isinstance(components, dict):
        for name, override in components.items():
            image = override.get("image") if isinstance(override, dict) else None
            if isinstance(image, dict) and (default_image := component_image_defaults(name, data.get("images"))):
                override["image"] = deep_merge(default_image.copy(), image)
    return data


def validate_deployment[M: MonitoringDeployment](
    data: dict,
    model: type[M] = MonitoringDeployment,
    *,
    source: str = "Monitoring configuration",
    explicit: bool = False,
) -> M:
    """Validate a profile. ``explicit`` also requires every metric, and every option, to be written out."""
    data = _complete_component_images(data)
    errors = []
    if explicit:
        groups = data.get("metrics")
        groups = groups if isinstance(groups, dict) else {}
        listed = {
            name: groups[GROUP_OF[name]][profile_key(name)]
            for name in Metrics.model_fields
            if isinstance(groups.get(GROUP_OF[name]), dict) and profile_key(name) in groups[GROUP_OF[name]]
        }
        missing = [name for name in Metrics.model_fields if name not in listed]
        errors += [f"{metric_path(name)}: write true, false or its options" for name in missing]
        errors += [
            f"{metric_path(name)}: list every option, e.g. {profile_key(name)}: {_options_example(name, value)}"
            for name, value in listed.items()
            if not _lists_all_options(name, value)
        ]
    try:
        config = model.model_validate(data)
    except ValidationError as exc:
        errors.extend(_error_line(error) for error in exc.errors())
        raise ValueError(_report(source, errors)) from None
    if errors:
        raise ValueError(_report(source, errors))
    return config


def _option_names(name: str) -> list[str]:
    """Options of a metric, in declaration order (empty for metrics that are just on or off)."""
    model = next(arg for arg in get_args(Metrics.model_fields[name].annotation) if arg is not type(None))
    return list(model.model_fields)


def _lists_all_options(name: str, value: Any) -> bool:
    """Off, a metric without options, or a mapping that names every option."""
    options = _option_names(name)
    if value is False or value is None or not options:
        return True
    return isinstance(value, dict) and set(options) <= set(value)


def _options_example(name: str, value: Any) -> str:
    """The flow mapping to write: given values kept, other options at their default."""
    model = next(arg for arg in get_args(Metrics.model_fields[name].annotation) if arg is not type(None))
    given = value if isinstance(value, dict) else {}
    parts = []
    for option, field in model.model_fields.items():
        if option in given:
            shown = given[option]
        elif field.is_required():
            parts.append(f"{option}: <{option}>")
            continue
        else:
            shown = field.get_default(call_default_factory=True)
        parts.append(f"{option}: {_flow(shown)}")
    return "{" + ", ".join(parts) + "}"


def _flow(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(_flow(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_flow(v)}" for k, v in value.items()) + "}"
    return str(value)


def _error_line(error: Any) -> str:
    """One pydantic error as ``where: what``, with metrics named by their place in the profile."""
    loc = [str(part) for part in error["loc"]]
    if len(loc) > 1 and loc[0] == "metrics" and loc[1] in GROUP_OF:
        loc = [metric_path(loc[1]), *loc[2:]]
    where = ".".join(loc) or "profile"
    message = error["msg"].removeprefix("Value error, ")
    return message if message.startswith((f"{where}.", f"{where}:")) else f"{where}: {message}"


def _report(source: str, errors: list[str]) -> str:
    return f"{source}:\n  - " + "\n  - ".join(errors)
