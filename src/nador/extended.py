"""Optional research collectors. Disabled independently of the original ML probes."""

import re
from ipaddress import IPv4Address, IPv6Address
from typing import Literal

from metamodel.models.base import StrictBaseModel
from pydantic import Field, model_validator
from .probe_catalog import PROBES, enabled_probes


class ReachabilityTarget(StrictBaseModel):
    """An explicit, bounded transport check from the collector pod."""

    name: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,63}$")
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    transport: Literal["tcp", "sctp", "pfcp_heartbeat"] = "tcp"
    source_category: Literal["core", "ran"] | None = None
    source_component: str | None = Field(default=None, min_length=1, max_length=63)
    addresses: list[IPv4Address | IPv6Address] = Field(default_factory=list, max_length=16)
    isolated_test_peer: bool = False

    @model_validator(mode="after")
    def validate_origin(self):
        if bool(self.source_category) != bool(self.source_component):
            raise ValueError("Source checks require both source_category and source_component")
        if (self.source_component or self.transport != "tcp") and not self.addresses:
            raise ValueError("Source-NF and non-TCP checks require explicit destination address pins")
        if self.transport == "pfcp_heartbeat" and (self.source_component or not self.isolated_test_peer):
            raise ValueError("PFCP heartbeat is restricted to an isolated test peer from the collector")
        return self


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
    off_cpu: bool = False
    off_cpu_stacks: bool = False
    attributed_drops: bool = False
    qfi: bool = False
    tcp_flows: bool = False
    packet_concordance: bool = False
    packet_identity_secret: str | None = Field(default=None, pattern=r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$", max_length=253)
    packet_run_id: str = Field(default="experiment", min_length=1, max_length=128)
    packet_interfaces: list[str] = Field(default_factory=lambda: ["eth0"], min_length=1, max_length=4)
    packet_state_limit: int = Field(default=4096, ge=16, le=16384)
    packet_ttl_seconds: float = Field(default=30, ge=1, le=300)
    session_window_validation: bool = False
    session_max_gap_seconds: float = Field(default=10, ge=.1, le=300)
    reachability: list[ReachabilityTarget] = Field(default_factory=list, max_length=32)
    quality_queries: list[QualityQuery] = Field(default_factory=list, max_length=64)
    node_image: str | None = None
    service_image: str | None = None
    probe_images: dict[str, str] = Field(default_factory=dict)
    probe_targets: dict[str, list[str]] = Field(default_factory=dict)
    interval_seconds: float = Field(default=5, ge=1, le=300)
    timeout_seconds: float = Field(default=2, ge=.1, le=10)
    capture_limit: int = Field(default=8, ge=1, le=32)

    @property
    def node_enabled(self):
        return any((self.scheduler_wait, self.kernel_drops, self.network_queues,
                    self.socket_counters, self.split_signaling, self.off_cpu,
                    self.attributed_drops, self.qfi, self.tcp_flows, self.packet_concordance,
                    any(target.source_component for target in self.reachability)))

    @property
    def service_enabled(self):
        return self.quality or any(not target.source_component for target in self.reachability)

    @property
    def active(self):
        return self.node_enabled or self.service_enabled or self.session_window_validation

    @model_validator(mode="after")
    def validate_settings(self):
        if self.probe_images:
            unknown = self.probe_images.keys() - PROBES.keys()
            if unknown:
                raise ValueError(f"Unknown probe images: {sorted(unknown)}")
            if self.node_image or self.service_image:
                raise ValueError("Choose probe_images or the bundled node/service images, not both")
            images = [(True, self.probe_images.get(name)) for name in enabled_probes(self)]
        else:
            if self.probe_targets:
                raise ValueError("probe_targets requires independent probe images")
            images = [(self.node_enabled, self.node_image), (self.service_enabled, self.service_image)]
        if set(self.probe_targets) - PROBES.keys() or any(not items for items in self.probe_targets.values()):
            raise ValueError("probe_targets requires a nonempty list for each selected probe kind")
        for enabled, image in images:
            if enabled and (not image or not (":" in image.rsplit("/", 1)[-1] or "@sha256:" in image)):
                raise ValueError("Enabled extended collectors require an explicit published image tag or digest")
        for entries in (self.reachability, self.quality_queries):
            names = [item.name for item in entries]
            if len(set(names)) != len(names):
                raise ValueError("Extended target and query names must be unique")
        if self.timeout_seconds > self.interval_seconds:
            raise ValueError("Probe timeout must not exceed the polling interval")
        if (self.packet_concordance or self.qfi or self.tcp_flows) and not self.packet_identity_secret:
            raise ValueError("Packet and flow evidence requires an existing Kubernetes Secret with key 'key'")
        if len(set(self.packet_interfaces)) != len(self.packet_interfaces) or any(
            not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,15}", name) for name in self.packet_interfaces
        ):
            raise ValueError("Packet capture requires distinct, explicit Linux interface names")
        if self.off_cpu_stacks and not self.off_cpu:
            raise ValueError("off_cpu_stacks requires off_cpu")
        return self
