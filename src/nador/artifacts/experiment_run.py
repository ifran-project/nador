"""ExperimentRun: record when each part of a workload ran, then collect evidence for those windows.

A run writes timeline events for the whole campaign and for each ``section``. Collectors
(see ``collectors.py``) add evidence around it: they may ``start`` before the workload,
``stop`` after it, and ``collect`` data for the run's time range. A collector failure is recorded in
``experiment_metadata.json`` and never hides an exception raised by the workload.
"""

import asyncio
import json
import time
from collections.abc import Coroutine, Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self
from uuid import uuid4
from zoneinfo import ZoneInfo

from .collectors import Collector, PrometheusExport
from .timeline import finalize_timeline

if TYPE_CHECKING:
    from nador.deployment import MonitoringManagement


def _outcome(exc: BaseException | None) -> str:
    if exc is None:
        return "completed"
    return "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"


async def _complete(coroutine: Coroutine[Any, Any, Any]) -> Any:
    """Run a coroutine to the end even if the caller is cancelled, then re-raise the cancellation.

    Collectors start and stop remote processes; abandoning them halfway would leave
    those running with nothing to clean them up.
    """
    task = asyncio.ensure_future(coroutine)
    cancelled = None
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError as exc:
            cancelled = exc
    if cancelled is not None:
        if not task.cancelled():
            task.exception()  # mark retrieved: the cancellation is what the caller sees
        raise cancelled
    return task.result()


class ExperimentRun:
    """Record a workload as timed sections and collect monitoring evidence for it.

    Pass a monitoring manager (Prometheus is reached through an SSH tunnel) or a directly
    reachable ``prometheus_url`` to export metrics; pass neither to record the timeline only.
    Extra ``collectors`` run alongside. The workload and its cleanup belong to the caller.
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
        monitoring: "MonitoringManagement | None" = None,
        collectors: Iterable[Collector] = (),
    ) -> None:
        if monitoring is not None and prometheus_url is not None:
            raise ValueError("Supply monitoring or prometheus_url, not both")
        self.directory = Path(directory)
        self.name = name
        self.monitoring = monitoring
        self.queries_file = queries_file
        self.timezone = timezone
        self.zone = ZoneInfo(timezone)
        self.run_id = uuid4().hex
        self.collectors = list(collectors)
        if monitoring is not None or prometheus_url is not None:
            self.collectors.append(PrometheusExport(url=prometheus_url, queries_file=queries_file, step=step))
        self.started = 0.0
        self.metadata: dict[str, Any] = {}
        self._sections: set[str] = set()
        self._entered = False
        self._closed = False

    # --- recording ---------------------------------------------------------------------------------------------------

    def error(self, label: str, error: object) -> None:
        """Record a collection problem; it fails the run only if the workload itself succeeded."""
        self.metadata["collection_errors"].append(f"{label}: {error}")

    def write_json(self, relative_path: str | Path, data: Any) -> Path:
        """Write a JSON file under the run directory, creating folders as needed."""
        path = self.directory / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return path

    def save_json(self, filename: str | Path, data: Any) -> None:
        """Save a JSON artifact inside the active experiment directory."""
        self._require_active("Save artifacts")
        self.write_json(filename, data)

    @contextmanager
    def section(self, name: str) -> Iterator[None]:
        """Record the start, end and outcome of one named experiment section."""
        self._require_active("Use sections")
        if not name or not name.strip():
            raise ValueError("Section name cannot be empty")
        if name in self._sections:
            raise ValueError(f"Duplicate section {name!r}")
        self._sections.add(name)
        self._event("section", "start", name)
        status = "completed"
        try:
            yield
        except BaseException:
            status = "failed"
            raise
        finally:
            self._event("section", "end", name, status=status)

    def _require_active(self, action: str) -> None:
        if not self._entered or self._closed:
            raise RuntimeError(f"{action} inside async with {type(self).__name__}(...)")

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

    def _save_metadata(self) -> None:
        self.write_json("experiment_metadata.json", self.metadata)

    # --- lifecycle ---------------------------------------------------------------------------------------------------

    async def _each(self, step: str, collectors: Iterable[Collector], *args: float) -> None:
        for collector in collectors:
            try:
                await getattr(collector, step)(self, *args)
            except Exception as exc:
                self.error(f"{collector.label} {step}", exc)

    async def __aenter__(self) -> Self:
        """Create the run directory, record the campaign start and start the collectors."""
        if self._entered:
            raise RuntimeError(f"{type(self).__name__} cannot be entered twice")
        self.directory.mkdir(parents=True, exist_ok=False)
        self._entered = True
        self.started = time.time()
        self.metadata = {
            "run_id": self.run_id,
            "name": self.name,
            "timezone": self.timezone,
            "status": "running",
            "collection_errors": [],
        }
        self._event("campaign", "start", self.name)
        self._save_metadata()
        try:
            await _complete(self._each("start", self.collectors))
        except BaseException as exc:
            await self.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        return self

    async def _finish(self) -> None:
        ended = time.time()
        self._event("campaign", "end", self.name, status=self.metadata["status"])
        await self._each("stop", reversed(self.collectors))
        try:
            finalize_timeline(
                self.directory / "timeline_events.jsonl", self.directory, run_id=self.run_id, timezone=self.timezone
            )
        except Exception as error:
            self.error("timeline", error)
        await self._each("collect", self.collectors, self.started, max(ended, self.started + 0.001))

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> bool:
        """Record the outcome, stop the collectors and collect evidence, even when the workload failed."""
        self._closed = True
        self.metadata["status"] = _outcome(exc)
        if exc:
            self.metadata["error"] = str(exc) or type(exc).__name__
        try:
            await _complete(self._finish())
        except asyncio.CancelledError:
            self.metadata["status"] = "cancelled"
            raise
        finally:
            self._save_metadata()
        if not exc and self.metadata["collection_errors"]:
            raise RuntimeError(f"Artifact collection incomplete; see {self.directory / 'experiment_metadata.json'}")
        return False
