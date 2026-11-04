"""Python experiment windows with guaranteed timeline finalization."""

import asyncio
import json
import time
from collections.abc import Iterator
from contextlib import AsyncExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from uuid import uuid4
from zoneinfo import ZoneInfo

from orchestrator_monitoring.deployment import MonitoringManagement

from . import prometheus_range_export as prometheus
from . import split_prometheus_by_timeline as splitter
from . import timeline_finalize as timeline
from .connection import prometheus_connection


class ExperimentRun:
    """Record native Python workloads and reuse the established artifact schemas.

    Pass a monitoring manager for automatic Prometheus access during export,
    or supply a directly reachable prometheus_url. Network workloads and their
    cleanup are owned by their caller.
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        name: str = "experiment",
        prometheus_url: str | None = None,
        queries_file: str | Path | None = None,
        step: str = "1s",
        timezone: str = "Europe/Paris",
        monitoring: MonitoringManagement | None = None,
    ) -> None:
        if monitoring is not None and prometheus_url is not None:
            raise ValueError("Supply monitoring or prometheus_url, not both")
        self.monitoring = monitoring
        self.directory = Path(directory)
        self.name = name
        self.prometheus_url = prometheus_url
        self.queries_file = queries_file
        self.step = step
        self.timezone = timezone
        self.zone = ZoneInfo(timezone)
        self.run_id = uuid4().hex
        self.names = set()
        self.entered = False
        self.active_sections = 0

    def _event(self, level: str, phase: str, name: str, **extra: Any) -> None:
        now = datetime.now(UTC)
        event = {
            "run_id": self.run_id,
            "level": level,
            "phase": phase,
            "name": name,
            "epoch": now.timestamp(),
            "utc_time": now.isoformat(),
            "local_time": now.astimezone(self.zone).isoformat(),
            "timezone": self.timezone,
            **extra,
        }
        with (self.directory / "timeline_events.jsonl").open("a") as f:
            f.write(json.dumps(event) + "\n")

    async def __aenter__(self) -> Self:
        if self.entered:
            raise RuntimeError("ExperimentRun cannot be entered twice")
        self.directory.mkdir(parents=True, exist_ok=False)
        self.entered = True
        self.started = time.time()
        self._event("campaign", "start", self.name)
        self.metadata = {
            "run_id": self.run_id,
            "name": self.name,
            "timezone": self.timezone,
            "status": "running",
            "collection_errors": [],
        }
        self._save_metadata()
        return self

    def _save_metadata(self) -> None:
        (self.directory / "experiment_metadata.json").write_text(json.dumps(self.metadata, indent=2))

    def save_json(self, filename: str | Path, data: Any) -> None:
        """Save a JSON artifact inside the active experiment directory."""
        if not self.entered or getattr(self, "closed", False):
            raise RuntimeError("Save artifacts inside async with ExperimentRun(...)")
        (self.directory / filename).write_text(json.dumps(data, indent=2))

    @contextmanager
    def section(self, name: str) -> Iterator[None]:
        """Record the start, end and outcome of one named experiment section."""
        if not self.entered or getattr(self, "closed", False):
            raise RuntimeError("Use sections inside async with ExperimentRun(...)")
        if name in self.names:
            raise ValueError(f"Duplicate section {name!r}")
        if not name or not name.strip():
            raise ValueError("Section name cannot be empty")
        self.names.add(name)
        self.active_sections += 1
        self._event("section", "start", name)
        status = "completed"
        try:
            yield
        except BaseException:
            status = "failed"
            raise
        finally:
            self._event("section", "end", name, status=status)
            self.active_sections -= 1

    def _collect(self, ended: float) -> None:
        errors = self.metadata["collection_errors"]
        try:
            timeline.main(
                [
                    "--timeline-jsonl",
                    str(self.directory / "timeline_events.jsonl"),
                    "--out-dir",
                    str(self.directory),
                    "--run-id",
                    self.run_id,
                    "--timezone",
                    self.timezone,
                ]
            )
        except Exception as exc:
            errors.append(f"timeline: {exc}")
        if self.prometheus_url:
            try:
                summary = self.directory / "prometheus_export_summary.json"
                args = [
                    "--prometheus-url",
                    self.prometheus_url,
                    "--start",
                    str(self.started),
                    "--end",
                    str(max(ended, self.started + 0.001)),
                    "--step",
                    self.step,
                    "--out",
                    str(self.directory / "prometheus_timeseries.csv"),
                    "--summary-json",
                    str(summary),
                    "--no-discover-metrics",
                ]
                if self.queries_file:
                    args += ["--queries-json", str(self.queries_file)]
                prometheus.main(args)
                queries = json.loads(summary.read_text())["queries"]
                errors.extend(f"{q['name']}: {q['error']}" for q in queries if q.get("error"))
                if not any(q.get("rows") for q in queries):
                    errors.append("Prometheus returned no samples")
                splitter.main(
                    [
                        "--full-csv",
                        str(self.directory / "prometheus_timeseries.csv"),
                        "--timeline-json",
                        str(self.directory / "timeline_summary.json"),
                        "--out-dir",
                        str(self.directory / "by_window"),
                        "--window-types",
                        "campaign,section",
                        "--compress",
                    ]
                )
            except Exception as exc:
                errors.append(f"prometheus: {exc}")

    async def _finalize(self, ended: float) -> None:
        try:
            async with AsyncExitStack() as access:
                if self.monitoring is not None:
                    try:
                        self.prometheus_url = await access.enter_async_context(prometheus_connection(self.monitoring))
                    except Exception as exc:
                        self.prometheus_url = None
                        self.metadata["collection_errors"].append(f"prometheus access: {exc}")
                await asyncio.to_thread(self._collect, ended)
        except Exception as exc:
            self.metadata["collection_errors"].append(f"collection: {exc}")

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> bool:
        self.closed = True
        self._event("campaign", "end", self.name, status="failed" if exc else "completed")
        self.metadata["status"] = "failed" if exc else "completed"
        if exc:
            self.metadata["error"] = str(exc) or type(exc).__name__
        ended = time.time()
        collection = asyncio.create_task(self._finalize(ended))
        try:
            await asyncio.shield(collection)
        except asyncio.CancelledError:
            await collection
            self.metadata["status"] = "cancelled"
            raise
        finally:
            self._save_metadata()
        if not exc and self.metadata["collection_errors"]:
            raise RuntimeError(f"Artifact collection incomplete; see {self.directory / 'experiment_metadata.json'}")
        return False
