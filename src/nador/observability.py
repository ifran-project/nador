"""Optional protocol, path and infrastructure collection settings."""

from typing import Self

from metamodel_3gpp.models.base import StrictBaseModel
from pydantic import Field, model_validator


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
        return self.ngap or self.pfcp or self.mapper_coverage or self.user_plane_paths

    @model_validator(mode="after")
    def require_collector_images(self) -> Self:
        for field in ("amf_image", "smf_image", "mapper_image"):
            image = getattr(self, field)
            if image is not None and not image.strip():
                raise ValueError(f"{field} must be a non-empty image or null to inherit the default")
        if self.path_port in range(9100, 9200) or self.path_port in range(9300, 9400):
            raise ValueError("path_port overlaps the latency/throughput exporter ranges; use 9403 or another range")
        return self
