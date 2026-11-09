"""Optional protocol, path and infrastructure collection settings."""
import json
from pathlib import Path
from typing import Self

from metamodel.models.base import StrictBaseModel
from pydantic import Field, model_validator

_SELECTED_IMAGES = json.loads(
    (Path(__file__).parent / "assets" / "observability_images.json").read_text()
)["images"]


class ObservabilitySettings(StrictBaseModel):
    """Opt-in collectors with verified ML image selections and explicit overrides."""

    ngap: bool = False
    pfcp: bool = False
    mapper_coverage: bool = False
    user_plane_paths: bool = False
    node_metrics: bool = False
    amf_image: str | None = _SELECTED_IMAGES["amf_image"]
    smf_image: str | None = _SELECTED_IMAGES["smf_image"]
    mapper_image: str | None = _SELECTED_IMAGES["mapper_image"]
    path_image: str | None = _SELECTED_IMAGES["path_image"]
    ngap_port: int = Field(default=9102, ge=1, le=65535)
    pfcp_port: int = Field(default=9104, ge=1, le=65535)
    path_port: int = Field(default=9403, ge=1, le=65535)
    pending_timeout_seconds: float = Field(default=30, gt=0, le=3600)
    path_interval_seconds: float = Field(default=5, ge=.1, le=3600)

    @property
    def active(self) -> bool:
        return self.ngap or self.pfcp or self.mapper_coverage or self.user_plane_paths

    @model_validator(mode="after")
    def require_collector_images(self) -> Self:
        for enabled, image in [(self.ngap, self.amf_image), (self.pfcp, self.smf_image),
                               (self.mapper_coverage, self.mapper_image), (self.user_plane_paths, self.path_image)]:
            if enabled and (not image or not image.strip()):
                raise ValueError("Enabled observability collectors require their matching image")
        if self.path_port in range(9100, 9200) or self.path_port in range(9300, 9400):
            raise ValueError("path_port overlaps the latency/throughput exporter ranges; use 9403 or another range")
        return self
