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

from nador.deployment import MonitoringManagement
from nador.probes import enabled_probes

from .prometheus_export import export_ranges, load_queries
from .split_by_window import split_windows
from .ssh_tunnel import prometheus_connection
from .timeline import finalize_timeline


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
        timezone: str = "UTC",
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
        self.closed = False

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
        """Create the run directory and record the campaign start."""
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
        if not self.entered or self.closed:
            raise RuntimeError("Save artifacts inside async with ExperimentRun(...)")
        (self.directory / filename).write_text(json.dumps(data, indent=2))

    @contextmanager
    def section(self, name: str) -> Iterator[None]:
        """Record the start, end and outcome of one named experiment section."""
        if not self.entered or self.closed:
            raise RuntimeError("Use sections inside async with ExperimentRun(...)")
        if name in self.names:
            raise ValueError(f"Duplicate section {name!r}")
        if not name or not name.strip():
            raise ValueError("Section name cannot be empty")
        self.names.add(name)
        self._event("section", "start", name)
        status = "completed"
        try:
            yield
        except BaseException:
            status = "failed"
            raise
        finally:
            self._event("section", "end", name, status=status)

    def _export_queries(self) -> list[dict[str, str]]:
        """Preserve the original profile and add opt-in extended evidence."""
        queries = load_queries(self.queries_file)
        if self.monitoring is None:
            return queries
        settings = self.monitoring.deployment_model.monitoring.extended
        if not enabled_probes(settings):
            return queries
        name = "nador_extended_raw"
        query = '{__name__=~"nador_.*"}'
        existing = next((item for item in queries if item["name"] == name), None)
        if existing is not None and existing["query"] != query:
            raise ValueError("nador_extended_raw query name is reserved for extended evidence")
        if existing is None:
            queries.append({"name": name, "query": query})
        (self.directory / "extended_export_queries.json").write_text(json.dumps(queries, indent=2))
        (self.directory / "extended_collector_config.json").write_text(
            json.dumps(settings.model_dump(mode="json", exclude={"packet_identity_secret"}), indent=2)
        )
        return queries

    def _collect(self, ended: float) -> None:
        errors = self.metadata["collection_errors"]
        try:
            finalize_timeline(
                self.directory / "timeline_events.jsonl", self.directory, run_id=self.run_id, timezone=self.timezone
            )
        except Exception as exc:
            errors.append(f"timeline: {exc}")
        if not self.prometheus_url:
            return
        try:
            summary = export_ranges(
                self.directory,
                url=self.prometheus_url,
                start=self.started,
                end=max(ended, self.started + 0.001),
                step=self.step,
                queries=self._export_queries(),
            )
            queries = summary["queries"]
            errors.extend(f"{q['name']}: {q['error']}" for q in queries if q.get("error"))
            if not any(q.get("rows") for q in queries):
                errors.append("Prometheus returned no samples")
            split_windows(self.directory)
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
        """Record the outcome and collect artifacts, even when the workload failed."""
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
