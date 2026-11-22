"""Turn the events ExperimentRun records into timeline windows, a CSV and a readable recap.

ExperimentRun writes one ``start`` and one ``end`` event per window (the whole
campaign, and each section). Pairing them gives the windows that the Prometheus
export is later split by.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from collections import defaultdict
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

WINDOW_FIELDS = [
    "window_id",
    "level",
    "name",
    "status",
    "start_epoch",
    "end_epoch",
    "duration_s",
    "start_local",
    "end_local",
    "timezone",
]


def safe_name(value: str) -> str:
    """Turn a label into a file-system-safe name."""
    value = re.sub(r"[^A-Za-z0-9_.=-]+", "_", value.strip())
    return value.strip("_") or "window"


def load_events(path: Path) -> list[dict]:
    """Read timeline events; malformed lines become parse_error events."""
    events = []
    if not path.exists():
        return events
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            events.append(
                {"level": "parse_error", "phase": "point", "line_no": line_no, "error": str(exc), "raw": line}
            )
            continue
        event["_line_no"] = line_no
        events.append(event)
    return events


def to_local(epoch: float, tz_name: str) -> str:
    """Format an epoch in a time zone, falling back to UTC."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    return datetime.fromtimestamp(epoch, tz).strftime("%Y-%m-%d %H:%M:%S %Z")


def duration_text(seconds: float) -> str:
    """Format seconds as 1h02m03s, 2m03s or 3s."""
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{sec:02d}s"
    if minutes:
        return f"{minutes}m{sec:02d}s"
    return f"{sec}s"


def make_window(start: dict, end: dict, tz_name: str) -> dict:
    """Build a timeline window from a start event and its end event."""
    start_epoch = float(start.get("epoch", 0))
    end_epoch = float(end.get("epoch", start_epoch))
    level, name = start.get("level", ""), start.get("name", "")
    return {
        "window_id": safe_name("__".join(part for part in (level, name) if part)),
        "level": level,
        "name": name,
        "status": end.get("status", ""),
        "start_epoch": start_epoch,
        "end_epoch": end_epoch,
        "duration_s": max(0, end_epoch - start_epoch),
        "start_local": to_local(start_epoch, tz_name),
        "end_local": to_local(end_epoch, tz_name),
        "timezone": tz_name,
    }


def pair_windows(events: list[dict], tz_name: str) -> tuple[list[dict], list[dict], list[dict]]:
    """Pair start and end events into windows; also return unmatched events."""
    starts: dict[tuple, list[dict]] = defaultdict(list)
    windows = []
    unmatched_ends = []
    for event in sorted(events, key=lambda item: (float(item.get("epoch", 0)), item.get("_line_no", 0))):
        key = (event.get("level", ""), event.get("name", ""))
        if event.get("phase") == "start":
            starts[key].append(event)
        elif event.get("phase") == "end":
            if starts.get(key):
                windows.append(make_window(starts[key].pop(), event, tz_name))
            else:
                unmatched_ends.append(event)
    unmatched_starts = [event for stack in starts.values() for event in stack]
    windows.sort(key=lambda row: (row["start_epoch"], row["end_epoch"], row["level"]))
    return windows, unmatched_starts, unmatched_ends


def write_csv(path: Path, rows: list[dict]) -> None:
    """Write windows with the fixed WINDOW_FIELDS columns."""
    with path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=WINDOW_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _span(window: dict) -> str:
    return f"{window['start_local']} -> {window['end_local']} ({duration_text(window['duration_s'])})"


def recap_lines(run_id: str, tz_name: str, windows: list[dict]) -> list[str]:
    """Human-readable recap of the campaign and its sections."""
    lines = [f"Timeline recap for {run_id} ({tz_name})"]
    campaign = next((w for w in windows if w["level"] == "campaign"), None)
    if campaign:
        lines.append(f"Campaign: {_span(campaign)}")
    sections = [w for w in windows if w["level"] == "section"]
    if sections:
        lines.append("Sections:")
        for section in sections:
            status = f" [{section['status']}]" if section["status"] not in ("", "completed") else ""
            lines.append(f"- section {section['name']}{status}: {_span(section)}")
    return lines


def write_markdown(path: Path, lines: list[str], summary: dict) -> None:
    """Write the recap and a guide to the output files as Markdown."""
    content = [
        "# Experiment Timeline",
        "",
        *lines,
        "",
        "## Output Files",
        "",
        "- `timeline_summary.csv`: paired campaign and section windows.",
        "- `timeline_summary.json`: same data for scripts.",
        "- `timeline_recap.txt`: terminal-friendly recap.",
        "- `by_window/campaign/*/`: full-campaign Prometheus samples, when exported.",
        "- `by_window/section/*/`: per-section Prometheus samples, when exported.",
        "",
        f"Unmatched starts: {len(summary['unmatched_starts'])}",
        f"Unmatched ends: {len(summary['unmatched_ends'])}",
        "",
    ]
    path.write_text("\n".join(content), encoding="utf-8")


def finalize_timeline(events_file: Path, out_dir: Path, *, run_id: str, timezone: str) -> None:
    """Write the timeline summary and recap from the recorded events."""
    out_dir.mkdir(parents=True, exist_ok=True)
    events = load_events(events_file)
    windows, unmatched_starts, unmatched_ends = pair_windows(events, timezone)
    run_id = run_id or (events[0].get("run_id", "") if events else "")
    summary = {
        "run_id": run_id,
        "timezone": timezone,
        "events": events,
        "windows": windows,
        "unmatched_starts": unmatched_starts,
        "unmatched_ends": unmatched_ends,
    }
    (out_dir / "timeline_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    write_csv(out_dir / "timeline_summary.csv", windows)
    lines = recap_lines(run_id, timezone, windows)
    (out_dir / "timeline_recap.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_markdown(out_dir / "timeline_summary.md", lines, summary)
    logger.info("%s", "\n".join(lines))
    if unmatched_starts or unmatched_ends:
        logger.warning("Unmatched timeline events: %d starts, %d ends", len(unmatched_starts), len(unmatched_ends))
