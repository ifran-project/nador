"""Optional research collectors. Disabled independently of the original ML probes."""

from typing import Literal

from metamodel_3gpp.models.base import StrictBaseModel
from pydantic import Field, model_validator

from .probe_catalog import PROBES, enabled_probes


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
    probe_images: dict[str, str] = Field(default_factory=dict)
    probe_targets: dict[str, list[str]] = Field(default_factory=dict)
    interval_seconds: float = Field(default=5, ge=1, le=300)
    timeout_seconds: float = Field(default=2, ge=0.1, le=10)
    capture_limit: int = Field(default=8, ge=1, le=32)

    @model_validator(mode="after")
    def validate_settings(self):
        unknown = self.probe_images.keys() - PROBES.keys()
        if unknown:
            raise ValueError(f"Unknown probe images: {sorted(unknown)}")
        missing = set(enabled_probes(self)) - self.probe_images.keys()
        if missing:
            raise ValueError(f"extended.probe_images requires an image for: {', '.join(sorted(missing))}")
        for name, image in self.probe_images.items():
            if ":" not in image.rsplit("/", 1)[-1] or any(c.isspace() for c in image):
                raise ValueError(f"extended.probe_images.{name} requires a published image tag or digest")
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
