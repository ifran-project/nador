"""Optional research collectors. Disabled independently of the original ML probes."""

from typing import Literal

from metamodel.models.base import StrictBaseModel
from pydantic import Field, model_validator


class ReachabilityTarget(StrictBaseModel):
    """An explicit, bounded transport check from the collector pod."""

    name: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,63}$")
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    transport: Literal["tcp"] = "tcp"


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
    reachability: list[ReachabilityTarget] = Field(default_factory=list, max_length=32)
    quality_queries: list[QualityQuery] = Field(default_factory=list, max_length=64)
    node_image: str | None = None
    service_image: str | None = None
    interval_seconds: float = Field(default=5, ge=1, le=300)
    timeout_seconds: float = Field(default=2, ge=.1, le=10)
    capture_limit: int = Field(default=8, ge=1, le=32)

    @property
    def node_enabled(self):
        return any((self.scheduler_wait, self.kernel_drops, self.network_queues,
                    self.socket_counters, self.split_signaling))

    @property
    def active(self):
        return self.node_enabled or self.quality or bool(self.reachability)

    @model_validator(mode="after")
    def validate_settings(self):
        for enabled, image in ((self.node_enabled, self.node_image),
                               (self.quality or bool(self.reachability), self.service_image)):
            if enabled and (not image or not (":" in image.rsplit("/", 1)[-1] or "@sha256:" in image)):
                raise ValueError("Enabled extended collectors require an explicit published image tag or digest")
        for entries in (self.reachability, self.quality_queries):
            names = [item.name for item in entries]
            if len(set(names)) != len(names):
                raise ValueError("Extended target and query names must be unique")
        if self.timeout_seconds > self.interval_seconds:
            raise ValueError("Probe timeout must not exceed the polling interval")
        return self
