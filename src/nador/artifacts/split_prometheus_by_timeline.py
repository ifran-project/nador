"""Split Prometheus samples into campaign and section folders."""

from __future__ import annotations

import csv
import gzip
import json
import logging
from contextlib import ExitStack
from pathlib import Path

from nador.artifacts.timeline_finalize import safe_name

logger = logging.getLogger(__name__)


def load_windows(path: Path, selected_levels: set[str]) -> list[dict]:
    """Read the timeline windows of the selected levels."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return [row for row in data.get("windows", []) if row.get("level") in selected_levels]


def window_subdir(window: dict) -> Path:
    """Folder of a window under by_window/."""
    return Path(window["level"]) / safe_name(window.get("window_id", "window"))


def to_float(value, default: float = 0.0) -> float:
    """Parse a float, falling back to a default."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def is_false(value) -> bool:
    """Whether a flag value means false (False, 0, no, off)."""
    if isinstance(value, bool):
        return value is False
    return str(value).strip().lower() in {"false", "0", "no", "off"}


def split_windows(directory: Path) -> None:
    """Write one compressed CSV and metadata file per recorded experiment window."""
    full_csv = directory / "prometheus_timeseries.csv"
    timeline_json = directory / "timeline_summary.json"
    out_dir = directory / "by_window"
    out_dir.mkdir(parents=True, exist_ok=True)

    selected_levels = {"campaign", "section"}
    summary_file = out_dir / "split_summary.json"
    for path, kind in ((full_csv, "Prometheus CSV"), (timeline_json, "timeline JSON")):
        if not path.exists():
            error = f"missing {kind}: {path}"
            summary_file.write_text(json.dumps({"error": error, "windows": []}, indent=2), encoding="utf-8")
            logger.warning(error)
            return

    windows = load_windows(timeline_json, selected_levels)
    skipped = [
        {
            "window_id": window.get("window_id"),
            "level": window.get("level"),
            "name": window.get("name"),
            "reason": "prometheus_disabled_for_window",
        }
        for window in windows
        if is_false(window.get("prometheus_enabled", ""))
    ]
    windows = [window for window in windows if not is_false(window.get("prometheus_enabled", ""))]
    if not windows:
        summary = {"error": "no matching windows", "windows": [], "skipped_windows": skipped}
        summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        logger.warning(summary["error"])
        return

    writers = []
    summary_windows = []
    with full_csv.open(newline="", encoding="utf-8") as source_fp, ExitStack() as output_files:
        reader = csv.DictReader(source_fp)
        if not reader.fieldnames:
            logger.warning("Empty Prometheus CSV: %s", full_csv)
            return

        for window in windows:
            subdir = out_dir / window_subdir(window)
            subdir.mkdir(parents=True, exist_ok=True)
            csv_path = subdir / "prometheus_timeseries.csv.gz"
            fp = output_files.enter_context(gzip.open(csv_path, "wt", newline="", encoding="utf-8"))
            writer = csv.DictWriter(fp, fieldnames=reader.fieldnames)
            writer.writeheader()
            start = to_float(window.get("start_epoch"))
            end = to_float(window.get("end_epoch"))
            metadata = {
                "window": window,
                "source_csv": str(full_csv),
                "padding_seconds": 0.0,
                "filter_start_epoch": start,
                "filter_end_epoch": end,
                "prometheus_csv": str(csv_path),
                "compressed": True,
                "rows": 0,
            }
            writers.append((start, end, writer, metadata, subdir))

        for row in reader:
            timestamp = to_float(row.get("timestamp"), -1.0)
            for start, end, writer, metadata, _subdir in writers:
                if start <= timestamp <= end:
                    writer.writerow(row)
                    metadata["rows"] += 1

    for _start, _end, _writer, metadata, subdir in writers:
        (subdir / "window_metadata.json").write_text(
            json.dumps(metadata, indent=2),
            encoding="utf-8",
        )
        summary_windows.append(
            {
                "window_id": metadata["window"].get("window_id"),
                "level": metadata["window"].get("level"),
                "scenario": metadata["window"].get("scenario"),
                "step": metadata["window"].get("step"),
                "direction": metadata["window"].get("direction"),
                "rows": metadata["rows"],
                "directory": str(subdir),
                "prometheus_csv": metadata["prometheus_csv"],
            }
        )

    summary = {
        "source_csv": str(full_csv),
        "timeline_json": str(timeline_json),
        "window_types": sorted(selected_levels),
        "padding_seconds": 0.0,
        "compressed": True,
        "windows": summary_windows,
        "skipped_windows": skipped,
    }
    summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    total_rows = sum(item["rows"] for item in summary_windows)
    logger.info("Split %s into %d windows with %d copied rows", full_csv, len(summary_windows), total_rows)
