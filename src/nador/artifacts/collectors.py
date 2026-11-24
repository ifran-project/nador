"""Collectors: evidence gathered around an ExperimentRun.

``Collector`` is the interface and ``RunOptions`` tunes the built-in collectors.
``PrometheusExport`` and ``LokiExport`` export the run's time range;
``cluster_collectors`` adds the ones that act on the cluster hosts.
"""

import asyncio
import math
import warnings
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Annotated
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nador.probes import enabled_probes

from .loki_export import OCUDU_PHY_DETAIL, PAGE_LIMIT, collect_logs, count_lines, log_query
from .prometheus_export import export_ranges, load_queries, split_windows
from .ssh_tunnel import loki_connection, prometheus_connection

if TYPE_CHECKING:
    from .experiment_run import ExperimentRun

# Every series the host probes export, added to the export when one of them runs.
HOST_PROBE_QUERY = {"name": "host_probes_raw", "query": '{__name__=~"nador_.*"}'}


class RunOptions(BaseModel):
    """How the built-in collectors of an ExperimentRun reach their data and how hard they poll."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    collect_setup: bool = True
    collect_ue_context: bool = True
    collect_csi: bool = False
    setup_hosts: list[Annotated[str, Field(min_length=1)]] | None = None
    mapper_url: str | None = None
    interval_seconds: float = Field(default=2, ge=0.1, le=3600, allow_inf_nan=False)
    timeout_seconds: int = Field(default=5, ge=1, le=60)
    max_seconds: int = Field(default=7200, ge=1)
    limit: int = Field(default=5000, ge=1)
    namespace: str | None = None
    service: str = "ue-mapper-api"
    service_port: int = Field(default=8080, ge=1, le=65535)
    loki_url: str | None = None
    loki_selector: str = '{namespace=~".+"}'
    loki_limit: int = Field(default=5000, ge=1, le=100_000)
    loki_max_queries: int = Field(default=1024, ge=1, le=100_000)
    loki_drop: list[Annotated[str, Field(min_length=1)]] = Field(default_factory=lambda: [OCUDU_PHY_DETAIL])
    loki_warn_lines: int = Field(default=100_000, ge=0)

    @field_validator("loki_drop")
    @classmethod
    def _check_loki_drop(cls, patterns: list[str]) -> list[str]:
        log_query("{}", patterns)  # refuses a pattern LogQL cannot quote
        return patterns

    @field_validator("mapper_url")
    @classmethod
    def _check_mapper_url(cls, url: str | None) -> str | None:
        if url:
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment:
                raise ValueError("mapper_url must be an HTTP(S) base URL without query or fragment")
        return url

    def sampler_args(self) -> list[str]:
        """Command-line options shared by the local and remote UE samplers."""
        fields = ("interval_seconds", "timeout_seconds", "max_seconds", "limit", "service", "service_port")
        return [arg for field in fields for arg in ("--" + field.replace("_", "-"), str(getattr(self, field)))]


class Collector:
    """Evidence gathered around a run. Override the steps you need; each one may raise."""

    label = "collector"

    async def start(self, run: "ExperimentRun") -> None:
        """Before the workload, right after the run directory is created."""

    async def stop(self, run: "ExperimentRun") -> None:
        """After the workload, in reverse order of start."""

    async def collect(self, run: "ExperimentRun", start: float, end: float) -> None:
        """After the timeline is written, for the run's time range."""


async def _open(stack: AsyncExitStack, url: str | None, connection, run: "ExperimentRun", label: str) -> str | None:
    """Use url, or tunnel to the service through the run's monitoring manager."""
    if url or run.monitoring is None:
        return url
    try:
        return await stack.enter_async_context(connection(run.monitoring))
    except Exception as exc:
        run.error(f"{label} access", exc)
        return None


class PrometheusExport(Collector):
    """Export query ranges to prometheus_timeseries.csv, then split the samples by timeline window."""

    label = "prometheus"

    def __init__(self, *, url: str | None, queries_file: str | Path | None, step: str) -> None:
        self.url = url
        self.queries_file = queries_file
        self.step = step

    def _queries(self, run: "ExperimentRun") -> list[dict[str, str]]:
        """The configured queries, plus every host-probe series when a host probe runs."""
        queries = load_queries(self.queries_file)
        if run.monitoring is not None and HOST_PROBE_QUERY["name"] not in {query["name"] for query in queries}:
            config = run.monitoring.deployment_model.monitoring.host_probe_config()
            if enabled_probes(config):
                queries.append(dict(HOST_PROBE_QUERY))
        return queries

    async def collect(self, run: "ExperimentRun", start: float, end: float) -> None:
        """Export metrics for the run and split them into campaign and section windows."""
        async with AsyncExitStack() as stack:
            url = await _open(stack, self.url, prometheus_connection, run, self.label)
            if url is None:
                return
            summary = await asyncio.to_thread(
                export_ranges,
                run.directory,
                url=url,
                start=start,
                end=end,
                step=self.step,
                queries=self._queries(run),
            )
        for query in summary["queries"]:
            if query.get("error"):
                run.error(query["name"], query["error"])
        if not any(query.get("rows") for query in summary["queries"]):
            run.error(self.label, "returned no samples")
        await asyncio.to_thread(split_windows, run.directory)


class LokiExport(Collector):
    """Export the run's log lines to logs/loki.jsonl, with an explicit completeness report.

    Lines matching a ``drop`` pattern are filtered out by Loki. Before exporting, Loki is asked how many lines
    remain: above ``warn_lines`` (0: never) a warning says how long the export may take and how to shorten it.
    """

    label = "loki"

    def __init__(
        self,
        *,
        url: str | None,
        selector: str,
        limit: int,
        max_queries: int,
        drop: list[str] | tuple[str, ...] = (OCUDU_PHY_DETAIL,),
        warn_lines: int = 100_000,
    ) -> None:
        self.url = url
        self.selector = selector
        self.limit = limit
        self.max_queries = max_queries
        self.drop = list(drop)
        self.warn_lines = warn_lines

    def _warn_if_large(self, lines: int | None) -> None:
        if lines is None or not self.warn_lines or lines <= self.warn_lines:
            return
        queries = math.ceil(lines / min(self.limit, PAGE_LIMIT))
        warnings.warn(
            f"Loki export: about {lines:,} log lines for this run, {queries:,}+ queries; this can take many minutes "
            f"(roughly 20 min for 400,000 lines). To shorten it, narrow loki_selector (e.g. one namespace), add "
            f"line patterns to loki_drop, or set loki_selector to a namespace without logs to skip it; "
            f"loki_warn_lines=0 silences this warning.",
            RuntimeWarning,
            stacklevel=2,
        )

    async def collect(self, run: "ExperimentRun", start: float, end: float) -> None:
        """Export logs and flag a possibly incomplete export."""
        async with AsyncExitStack() as stack:
            url = await _open(stack, self.url, loki_connection, run, self.label)
            if url is None:
                return
            if self.warn_lines:
                query = log_query(self.selector, self.drop)
                self._warn_if_large(await asyncio.to_thread(count_lines, url, query, start, end))
            summary = await asyncio.to_thread(
                collect_logs,
                run.directory / "logs",
                url=url,
                selector=self.selector,
                start=start,
                end=end,
                limit=self.limit,
                max_queries=self.max_queries,
                drop=self.drop,
            )
        if summary.get("possible_truncation"):
            budget = f"query budget exhausted ({summary['queries']}/{summary['max_queries']}); "
            detail = budget if summary.get("query_budget_exhausted") else ""
            run.error(self.label, detail + "possible truncation; log export may be incomplete")
