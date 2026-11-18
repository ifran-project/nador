"""Timeline pairing and summary files written after every experiment."""

import csv
import json

from nador.artifacts.timeline import WINDOW_FIELDS, finalize_timeline

T0 = 1_700_000_000.0


def write_events(path, events):
    path.write_text("".join(json.dumps(event) + "\n" for event in events))


def event(level, phase, name, offset, **extra):
    return {"run_id": "r1", "level": level, "phase": phase, "name": name, "epoch": T0 + offset, **extra}


def test_sections_pair_into_windows(tmp_path):
    events = tmp_path / "timeline_events.jsonl"
    write_events(
        events,
        [
            event("campaign", "start", "exp", 0),
            event("section", "start", "ping", 1),
            event("section", "end", "ping", 11, status="completed"),
            event("section", "start", "iperf", 12),
            event("section", "end", "iperf", 72, status="failed"),
            event("section", "start", "dangling", 73),
            event("campaign", "end", "exp", 80, status="completed"),
        ],
    )
    finalize_timeline(events, tmp_path, run_id="r1", timezone="UTC")

    summary = json.loads((tmp_path / "timeline_summary.json").read_text())
    windows = {(w["level"], w["name"]): w for w in summary["windows"]}
    assert set(windows) == {("campaign", "exp"), ("section", "ping"), ("section", "iperf")}
    assert windows[("section", "iperf")]["duration_s"] == 60
    assert [e["name"] for e in summary["unmatched_starts"]] == ["dangling"]

    with (tmp_path / "timeline_summary.csv").open() as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0]) == WINDOW_FIELDS
    assert len(rows) == 3

    recap = (tmp_path / "timeline_recap.txt").read_text().splitlines()
    assert recap == [
        "Timeline recap for r1 (UTC)",
        "Campaign: 2023-11-14 22:13:20 UTC -> 2023-11-14 22:14:40 UTC (1m20s)",
        "Sections:",
        "- section ping: 2023-11-14 22:13:21 UTC -> 2023-11-14 22:13:31 UTC (10s)",
        "- section iperf: 2023-11-14 22:13:32 UTC -> 2023-11-14 22:14:32 UTC (1m00s)",
    ]


def test_malformed_lines_are_reported_not_fatal(tmp_path):
    events = tmp_path / "timeline_events.jsonl"
    events.write_text('{"level": "campaign", "phase": "start", "name": "x", "epoch": 1}\nnot json\n')
    finalize_timeline(events, tmp_path, run_id="r1", timezone="UTC")
    summary = json.loads((tmp_path / "timeline_summary.json").read_text())
    assert any(e["level"] == "parse_error" for e in summary["events"])
