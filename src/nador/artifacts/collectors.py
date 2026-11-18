"""Collectors: evidence gathered around an ExperimentRun.

``Collector`` is the interface. ``PrometheusExport`` and ``LokiExport`` export the
run's time range; ``observability_run`` adds collectors that act on the cluster hosts.
"""

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING

from nador.probes import enabled_probes

from .loki_export import collect_logs
from .prometheus_export import export_ranges, load_queries
from .split_by_window import split_windows
from .ssh_tunnel import loki_connection, prometheus_connection

if TYPE_CHECKING:
    from .experiment_run import ExperimentRun

EXTENDED_QUERY = {"name": "nador_extended_raw", "query": '{__name__=~"nador_.*"}'}


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
        """The configured queries, plus raw extended-probe metrics when those probes are enabled."""
        queries = load_queries(self.queries_file)
        if run.monitoring is None:
            return queries
        settings = run.monitoring.deployment_model.monitoring.extended
        if not enabled_probes(settings):
            return queries
        existing = next((item for item in queries if item["name"] == EXTENDED_QUERY["name"]), None)
        if existing is not None and existing["query"] != EXTENDED_QUERY["query"]:
            raise ValueError("nador_extended_raw query name is reserved for extended evidence")
        if existing is None:
            queries.append(dict(EXTENDED_QUERY))
        run.write_json("extended_export_queries.json", queries)
        run.write_json(
            "extended_collector_config.json", settings.model_dump(mode="json", exclude={"packet_identity_secret"})
        )
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
    """Export the run's log lines to logs/loki.jsonl, with an explicit completeness report."""

    label = "loki"

    def __init__(self, *, url: str | None, selector: str, limit: int, max_queries: int) -> None:
        self.url = url
        self.selector = selector
        self.limit = limit
        self.max_queries = max_queries

    async def collect(self, run: "ExperimentRun", start: float, end: float) -> None:
        """Export logs and flag a possibly incomplete export."""
        async with AsyncExitStack() as stack:
            url = await _open(stack, self.url, loki_connection, run, self.label)
            if url is None:
                return
            summary = await asyncio.to_thread(
                collect_logs,
                run.directory / "logs",
                url=url,
                selector=self.selector,
                start=start,
                end=end,
                limit=self.limit,
                max_queries=self.max_queries,
            )
        if summary.get("possible_truncation"):
            budget = f"query budget exhausted ({summary['queries']}/{summary['max_queries']}); "
            detail = budget if summary.get("query_budget_exhausted") else ""
            run.error(self.label, detail + "possible truncation; log export may be incomplete")
