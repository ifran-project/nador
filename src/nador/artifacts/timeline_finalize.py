"""Build human- and machine-readable timeline summaries for scenario runs."""

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
    "scenario",
    "scenario_title",
    "step",
    "direction",
    "ue",
    "section_index",
    "runner_type",
    "parallel_streams",
    "traffic_type",
    "ping_count",
    "ping_interval",
    "ping_timeout",
    "ping_size",
    "start_epoch",
    "end_epoch",
    "duration_s",
    "start_local",
    "end_local",
    "timezone",
    "ues",
    "parallel_by_ue",
    "ue_roles",
    "directions",
    "directions_by_ue",
    "prometheus_enabled",
    "pcap_enabled",
    "artifacts_file",
    "artifact_overrides",
    "scenario_index",
    "step_index",
    "name",
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
                {
                    "level": "parse_error",
                    "phase": "point",
                    "line_no": line_no,
                    "error": str(exc),
                    "raw": line,
                }
            )
            continue
        event["_line_no"] = line_no
        events.append(event)
    return events


def event_key(event: dict) -> tuple:
    """Key that pairs a start event with its end event."""
    level = event.get("level", "")
    return (
        level,
        event.get("name", ""),
        event.get("scenario", ""),
        event.get("step", ""),
        event.get("direction", ""),
        str(event.get("scenario_index", "")),
        str(event.get("step_index", "")),
    )


def to_int(value, default: int = 0) -> int:
    """Parse an integer (accepting floats), falling back to a default."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def to_local(epoch: float, tz_name: str) -> str:
    """Format an epoch in a time zone, falling back to UTC."""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    return datetime.fromtimestamp(epoch, tz).strftime("%Y-%m-%d %H:%M:%S %Z")


def duration_text(seconds: int) -> str:
    """Format seconds as 1h02m03s, 2m03s or 3s."""
    seconds = max(0, int(seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{sec:02d}s"
    if minutes:
        return f"{minutes}m{sec:02d}s"
    return f"{sec}s"


def json_field(event: dict, field: str, default):
    """Read an event field, replacing null with a default."""
    value = event.get(field, default)
    return value if value is not None else default


def make_window(start: dict, end: dict, tz_name: str) -> dict:
    """Build a timeline window from a start event and its end event."""
    start_epoch = float(start.get("epoch", 0))
    end_epoch = float(end.get("epoch", start_epoch))
    level = start.get("level", "")
    scenario = start.get("scenario", "")
    step = start.get("step", "")
    direction = start.get("direction", "")
    name = start.get("name", "")
    parts = [level, *(str(item) for item in (scenario, step, direction, name) if item)]
    window_id = safe_name("__".join(parts))
    return {
        "window_id": window_id,
        "level": level,
        "scenario": scenario,
        "scenario_title": start.get("scenario_title") or start.get("title", ""),
        "step": step,
        "direction": direction,
        "ue": "",
        "section_index": to_int(start.get("section_index"), -1),
        "runner_type": start.get("runner_type", ""),
        "parallel_streams": to_int(start.get("parallel_streams"), 0),
        "traffic_type": start.get("traffic_type", "tcp"),
        "ping_count": start.get("ping_count", ""),
        "ping_interval": start.get("ping_interval", ""),
        "ping_timeout": start.get("ping_timeout", ""),
        "ping_size": start.get("ping_size", ""),
        "start_epoch": start_epoch,
        "end_epoch": end_epoch,
        "duration_s": max(0, end_epoch - start_epoch),
        "start_local": to_local(start_epoch, tz_name),
        "end_local": to_local(end_epoch, tz_name),
        "timezone": tz_name,
        "ues": json_field(start, "ues", []),
        "parallel_by_ue": json_field(start, "parallel_by_ue", {}),
        "ue_roles": json_field(start, "ue_roles", {}),
        "directions": json_field(start, "directions", []),
        "directions_by_ue": json_field(start, "directions_by_ue", {}),
        "prometheus_enabled": start.get("prometheus_enabled", ""),
        "pcap_enabled": start.get("pcap_enabled", ""),
        "artifacts_file": start.get("artifacts_file", ""),
        "artifact_overrides": json_field(start, "artifact_overrides", {}),
        "scenario_index": to_int(start.get("scenario_index"), -1),
        "step_index": to_int(start.get("step_index"), -1),
        "name": name,
    }


def pair_windows(events: list[dict], tz_name: str) -> tuple[list[dict], list[dict], list[dict]]:
    """Pair start and end events into windows; also return unmatched events."""
    starts: dict[tuple, list[dict]] = defaultdict(list)
    windows = []
    unmatched_ends = []

    for event in sorted(events, key=lambda item: (float(item.get("epoch", 0)), item.get("_line_no", 0))):
        phase = event.get("phase")
        if phase == "start":
            starts[event_key(event)].append(event)
        elif phase == "end":
            key = event_key(event)
            if starts.get(key):
                start = starts[key].pop()
                windows.append(make_window(start, event, tz_name))
            else:
                unmatched_ends.append(event)

    unmatched_starts = [event for stack in starts.values() for event in stack]
    windows.sort(key=lambda row: (row["start_epoch"], row["end_epoch"], row["level"]))
    return windows, unmatched_starts, unmatched_ends


def expand_iperf_tasks(windows: list[dict]) -> list[dict]:
    """Expand direction windows into one iperf or ping task per UE."""
    tasks = []
    for window in windows:
        if window.get("level") != "direction":
            continue
        traffic_type = str(window.get("traffic_type") or "tcp").lower()
        ues = window.get("ues") or []
        parallel_by_ue = window.get("parallel_by_ue") or {}
        directions_by_ue = window.get("directions_by_ue") or {}
        default_parallel = to_int(window.get("parallel_streams"), 1)
        for ue in ues:
            task = dict(window)
            task["level"] = "ping_task" if traffic_type == "icmp" else "iperf_task"
            task["ue"] = ue
            task["parallel_streams"] = to_int(parallel_by_ue.get(ue), default_parallel)
            if directions_by_ue:
                task["direction"] = directions_by_ue.get(ue, "unknown")
            task["window_id"] = safe_name(
                "__".join(
                    [
                        task["level"],
                        window.get("scenario", ""),
                        window.get("step", ""),
                        task.get("direction", ""),
                        ue,
                        f"P{task['parallel_streams']}"
                        if task["level"] == "iperf_task"
                        else f"count{task.get('ping_count', '')}",
                    ]
                )
            )
            tasks.append(task)
    return tasks


def csv_value(value):
    """Serialize lists and dicts as JSON for CSV cells."""
    if isinstance(value, (list, dict)):
        return json.dumps(value, sort_keys=True)
    return value


def write_csv(path: Path, rows: list[dict]) -> None:
    """Write windows with the fixed WINDOW_FIELDS columns."""
    with path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=WINDOW_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: csv_value(row.get(field, "")) for field in WINDOW_FIELDS})


def _span(window: dict) -> str:
    return f"{window['start_local']} -> {window['end_local']} ({duration_text(window['duration_s'])})"


def _section_line(section: dict) -> str:
    title = f" - {section['scenario_title']}" if section.get("scenario_title") else ""
    details = [f"runner={section['runner_type']}"] if section.get("runner_type") else []
    details += [
        f"{flag.removesuffix('_enabled')}={section.get(flag)}"
        for flag in ("prometheus_enabled", "pcap_enabled")
        if section.get(flag) != ""
    ]
    detail_text = f" [{' '.join(details)}]" if details else ""
    return f"- section {section['name']}{title}{detail_text}: {_span(section)}"


def _task_line(task: dict) -> str:
    if task.get("level") == "ping_task":
        return f"  - {task['step']} {task['ue']} ICMP ping count={task.get('ping_count', '')}: {_span(task)}"
    return f"  - {task['step']} {task['ue']} {task['direction']} -P{task['parallel_streams']}: {_span(task)}"


def recap_lines(run_id: str, tz_name: str, windows: list[dict], iperf_tasks: list[dict]) -> list[str]:
    """Human-readable recap of the campaign, sections and scenarios."""
    lines = [f"Timeline recap for {run_id} ({tz_name})"]
    campaign = next((w for w in windows if w["level"] == "campaign"), None)
    traffic = next((w for w in windows if w["level"] == "traffic_campaign"), None)
    if campaign:
        lines.append(f"Campaign: {_span(campaign)}")
    if traffic:
        lines.append(f"Traffic: {_span(traffic)}")
    sections = [w for w in windows if w["level"] == "section"]
    if sections:
        lines.append("Sections:")
        lines.extend(_section_line(section) for section in sections)
    for scenario in (w for w in windows if w["level"] == "scenario"):
        title = f" - {scenario['scenario_title']}" if scenario.get("scenario_title") else ""
        lines.append(f"- {scenario['scenario']}{title}: {_span(scenario)}")
        lines.extend(_task_line(t) for t in iperf_tasks if t.get("scenario") == scenario.get("scenario"))
    return lines


def write_markdown(path: Path, lines: list[str], summary_json: dict) -> None:
    """Write the recap and a guide to the output files as Markdown."""
    content = ["# Experiment Timeline", ""]
    content.extend(lines)
    content.extend(
        [
            "",
            "## Output Files",
            "",
            "- `timeline_summary.csv`: paired campaign, section and task windows.",
            "- `timeline_summary.json`: same data for scripts.",
            "- `timeline_recap.txt`: terminal-friendly recap.",
            "- `by_window/campaign/*/`: full-campaign Prometheus samples, when exported.",
            "- `by_window/section/*/`: per-section Prometheus samples, when exported.",
            "",
            f"Unmatched starts: {len(summary_json.get('unmatched_starts', []))}",
            f"Unmatched ends: {len(summary_json.get('unmatched_ends', []))}",
            "",
        ]
    )
    path.write_text("\n".join(content), encoding="utf-8")


def finalize_timeline(events_file: Path, out_dir: Path, *, run_id: str, timezone: str) -> None:
    """Write the timeline summary and recap from the recorded events."""
    out_dir.mkdir(parents=True, exist_ok=True)
    events = load_events(events_file)
    windows, unmatched_starts, unmatched_ends = pair_windows(events, timezone)
    iperf_tasks = expand_iperf_tasks(windows)
    all_rows = windows + iperf_tasks

    run_id = run_id or (events[0].get("run_id", "") if events else "")
    summary = {
        "run_id": run_id,
        "timezone": timezone,
        "events": events,
        "windows": windows,
        "traffic_tasks": iperf_tasks,
        "iperf_tasks": iperf_tasks,
        "unmatched_starts": unmatched_starts,
        "unmatched_ends": unmatched_ends,
    }

    (out_dir / "timeline_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    write_csv(out_dir / "timeline_summary.csv", all_rows)
    lines = recap_lines(run_id, timezone, windows, iperf_tasks)
    (out_dir / "timeline_recap.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_markdown(out_dir / "timeline_summary.md", lines, summary)
    logger.info("%s", "\n".join(lines))
    if unmatched_starts or unmatched_ends:
        logger.warning("Unmatched timeline events: %d starts, %d ends", len(unmatched_starts), len(unmatched_ends))
