"""NADOR's metric registry: what each metric means, its unit, which way is good, and how to read it.

The registry (``assets/metric_registry.yaml``) describes the metrics a NADOR deployment exposes:

- the 5G and probe metrics one by one, under the name to query (a histogram without ``_bucket``, ``_sum``,
  ``_count``), with the labels that identify a UE;
- the infrastructure metrics (node, container, Kubernetes, monitoring) by name prefix;
- NADOR's named queries (``assets/queries.json``), with the metrics they read;
- how a per-UE series is tied to a UE (``ue_identity``).

Every entry says whether a person has checked it (``verified``). For example::

    from nador.metric_registry import load_registry

    registry = load_registry()
    registry.describe("ue_dl_nof_nok").meaning      # 'Downlink transmissions not acknowledged (HARQ NACK)...'
    registry.describe("gtp_teid_latency_observed_ns_bucket").type  # 'histogram': the series of a histogram
    registry.describe("node_load1").prefix          # 'node_': an infrastructure family
"""

import re
from functools import cache
from pathlib import Path
from typing import Any, Literal

import yaml
from metamodel_3gpp.models.base import StrictBaseModel
from pydantic import Field

DEFAULT_REGISTRY_FILE = Path(__file__).parent / "assets" / "metric_registry.yaml"
Layer = Literal["ran", "core", "user_plane", "ue_identity", "host", "monitoring", "kubernetes"]
SERIES_SUFFIX = re.compile(r"_(bucket|sum|count)$")


class Metric(StrictBaseModel):
    """One 5G or probe metric."""

    kind: Literal["exporter", "probe", "computed"]
    layer: Layer
    component: str | None = None
    type: Literal["counter", "gauge", "histogram", "summary", "untyped"]
    unit: str | None = None
    meaning: str
    better: Literal["higher", "lower"] | None = Field(default=None, description="None when neither is better")
    ue_labels: list[str] = Field(default_factory=list, description="The labels that identify a UE")
    labels: list[str] = Field(default_factory=list, description="Its other labels, besides where it was scraped")
    use: str | None = Field(default=None, description="How to aggregate it in PromQL")
    source: str
    produced_by: str | None = Field(default=None, description="The profile metric that makes NADOR collect it")
    implementations: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list, description="Where the description comes from")
    verified: bool = False


class Family(StrictBaseModel):
    """The infrastructure metrics that start with one prefix."""

    prefix: str
    layer: Layer
    source: str
    produced_by: str
    meaning: str
    metrics: int = Field(description="How many metrics the catalog lists with this prefix")
    examples: list[str]
    verified: bool = False


class Query(StrictBaseModel):
    """One of NADOR's named queries."""

    meaning: str
    reads: list[str] = Field(description="The metrics it reads")
    by: list[str] = Field(default_factory=list, description="The labels it groups by")
    problems: list[str] = Field(default_factory=list, description="Why it returns nothing or wrong values")
    verified: bool = False


class MetricRegistry(StrictBaseModel):
    """The registry: metrics, families, queries, and how series are tied to UEs."""

    schema_: int = Field(alias="schema")
    about: dict[str, Any]
    ue_identity: dict[str, Any]
    metrics: dict[str, Metric]
    families: list[Family]
    queries: dict[str, Query]

    def describe(self, name: str) -> Metric | Family | None:
        """The entry of a metric or series name: itself, its histogram or summary, or its family; None if unknown.

        ``x_bucket``, ``x_sum`` and ``x_count`` are the series of the histogram or summary ``x``; a name the registry
        does not list one by one is described by the longest family prefix it starts with.
        """
        if name in self.metrics:
            return self.metrics[name]
        base = SERIES_SUFFIX.sub("", name)
        if base in self.metrics and self.metrics[base].type in ("histogram", "summary"):
            return self.metrics[base]
        matching = [family for family in self.families if name.startswith(family.prefix)]
        return max(matching, key=lambda family: len(family.prefix), default=None)

    def unverified(self) -> list[str]:
        """The metrics and queries no person has checked yet, as ``metrics.<name>`` and ``queries.<name>``."""
        sections = {"metrics": self.metrics, "queries": self.queries}
        return [
            f"{section}.{name}"
            for section, entries in sections.items()
            for name, e in entries.items()
            if not e.verified
        ]


@cache
def _load(path: Path) -> MetricRegistry:
    return MetricRegistry.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_registry(path: str | Path | None = None) -> MetricRegistry:
    """Read the metric registry (default: assets/metric_registry.yaml); each file is read once."""
    return _load(Path(path or DEFAULT_REGISTRY_FILE).resolve())
