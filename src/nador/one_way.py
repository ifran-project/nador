"""Opt-in, controller-managed one-way measurements."""

from ipaddress import IPv4Address
from typing import Self

from metamodel_3gpp.models.base import StrictBaseModel
from pydantic import Field, model_validator

from nador.images import TaggedImage


class N3Settings(StrictBaseModel):
    """One-way latency between gNB and UPF on N3."""

    enabled: bool = False
    log_samples: bool = False


class UpfTransitSettings(StrictBaseModel):
    """One-way transit latency through the UPF towards a test peer."""

    enabled: bool = False
    peer: IPv4Address | None = None


class OneWaySettings(StrictBaseModel):
    """Controller-managed one-way probes and their shared settings."""

    image: TaggedImage | None = None
    sample: int = Field(default=16, ge=1, le=0xFFFFFFFF)
    test_port: int = Field(default=0, ge=0, le=65535)
    n3: N3Settings = Field(default_factory=N3Settings)
    upf_transit: UpfTransitSettings = Field(default_factory=UpfTransitSettings)

    @model_validator(mode="after")
    def check_requirements(self) -> Self:
        """Enabled probes need an image; UPF transit needs a peer and port."""
        if (self.n3.enabled or self.upf_transit.enabled) and not self.image:
            raise ValueError("Enabled one-way probes require one_way.image")
        if self.upf_transit.enabled and (not self.upf_transit.peer or not self.test_port):
            raise ValueError("UPF transit requires upf_transit.peer and test_port")
        return self
