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

from fiveg_lens.csi.collection import CsiCollector
from fiveg_lens.deployment import MonitoringManagement

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
        csi: CsiCollector | list[CsiCollector] | None = None,
        csi_clock_threshold_s: float = 0.5,
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
        collectors = [] if csi is None else [csi] if isinstance(csi, CsiCollector) else list(csi)
        if len({collector.component for collector in collectors}) != len(collectors):
            raise ValueError("Give one CsiCollector per RAN component")
        self.csi = collectors
        # Offset (RAN node - local clock) is applied to the CSI windows only beyond this bound: below
        # it, the ssh measurement error (RTT/2) is of the same order as the offset itself.
        self.csi_clock_threshold_s = csi_clock_threshold_s
        self.csi_offset_s: dict[str, float] = {}

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
        # Clock of each CSI node, measured before the first event (one node per component)
        clocks = await asyncio.gather(*(collector.measure_clock() for collector in self.csi))
        for collector, clock in zip(self.csi, clocks, strict=True):
            offset = clock["offset_s"] if abs(clock["offset_s"]) > self.csi_clock_threshold_s else 0.0
            self.csi_offset_s[collector.component] = offset
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
        if self.csi:
            self.metadata["csi"] = {
                collector.component: {
                    "node": collector.node_name,
                    "clock_start": clock,
                    "clock_threshold_s": self.csi_clock_threshold_s,
                    "clock_offset_applied_s": self.csi_offset_s[collector.component],
                    "clock_offset_applied": self.csi_offset_s[collector.component] != 0.0,
                    "warnings": [],
                }
                for collector, clock in zip(self.csi, clocks, strict=True)
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
        # One CSI file per component, each on its own node: collected in parallel
        await asyncio.gather(*(self._collect_csi(collector) for collector in self.csi))

    def _csi_windows(self, collector: CsiCollector) -> Path | None:
        """Timeline windows on the clock of the collector's node: <run>/csi/<component>/csi_windows.json."""
        timeline = self.directory / "timeline_summary.json"
        if not timeline.is_file():
            return None
        offset = self.csi_offset_s[collector.component]
        windows = [
            window | {"ran_start_epoch": window["start_epoch"] + offset, "ran_end_epoch": window["end_epoch"] + offset}
            for window in json.loads(timeline.read_text())["windows"]
        ]
        path = self.directory / "csi" / collector.component / "csi_windows.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"node": collector.node_name, "offset_applied_s": offset, "windows": windows}))
        return path

    async def _collect_csi(self, collector: CsiCollector) -> None:
        errors = self.metadata["collection_errors"]
        record = self.metadata["csi"][collector.component]
        try:
            clock_end = await collector.measure_clock()
            record["clock_end"] = clock_end
            # Offset change over the run (clock drift or step); compare with the ~5 s CSI batch granularity
            drift = clock_end["offset_s"] - record["clock_start"]["offset_s"]
            record["clock_drift_s"] = drift
            if abs(drift) > self.csi_clock_threshold_s:
                record["warnings"].append(
                    f"{collector.node_name} clock offset changed by {drift:+.3f} s during the run "
                    f"(threshold {self.csi_clock_threshold_s} s): CSI windows may be shifted"
                )
        except Exception as exc:
            errors.append(f"csi {collector.component} clock: {exc}")
        try:
            record["summary"] = await collector.collect(
                self.directory / "csi" / collector.component, self._csi_windows(collector)
            )
        except Exception as exc:
            errors.append(f"csi {collector.component}: {exc}")

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
