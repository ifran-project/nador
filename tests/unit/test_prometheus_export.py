"""Exporting Prometheus ranges to CSV, and splitting the samples by experiment window."""

import csv
import gzip
import json

import pytest
from support import matrix

from nador.artifacts.prometheus_export import (
    DEFAULT_QUERIES_FILE,
    export_ranges,
    http_json,
    load_queries,
    split_windows,
)
from nador.artifacts.timeline import finalize_timeline

QUERIES = [{"name": "rtt", "query": "rate(rtt[5s])"}, {"name": "load", "query": "node_load1"}]


def rows(directory):
    with (directory / "prometheus_timeseries.csv").open() as f:
        return list(csv.DictReader(f))


def test_each_sample_becomes_a_row_with_its_labels(tmp_path, http):
    http.routes["/api/v1/query_range"] = lambda q: matrix(
        ({"__name__": "rtt", "imsi": "001010000000001"}, [(10, "1.5"), (20, "2.5")])
    )
    summary = export_ranges(tmp_path, url=http.url, start=0, end=30, step="5s", queries=QUERIES[:1])
    assert summary["queries"] == [{"name": "rtt", "query": "rate(rtt[5s])", "rows": 2}]
    exported = rows(tmp_path)
    assert [(r["timestamp"], r["value"], r["imsi"]) for r in exported] == [
        ("10", "1.5", "001010000000001"),
        ("20", "2.5", "001010000000001"),
    ]
    assert json.loads((tmp_path / "prometheus_export_summary.json").read_text())["step"] == "5s"


def test_the_range_and_step_are_sent_to_prometheus(tmp_path, http):
    http.routes["/api/v1/query_range"] = lambda q: matrix()
    export_ranges(tmp_path, url=http.url + "/", start=100, end=200, step="15s", queries=QUERIES[:1])
    assert http.requests == [
        ("/api/v1/query_range", {"query": "rate(rtt[5s])", "start": "100", "end": "200", "step": "15s"})
    ]


def test_a_failing_query_is_recorded_and_the_others_still_run(tmp_path, http):
    def answer(query):
        if query["query"] == "node_load1":
            return {"status": "error", "error": "bad query"}
        return matrix(({}, [(1, "1")]))

    http.routes["/api/v1/query_range"] = answer
    summary = export_ranges(tmp_path, url=http.url, start=0, end=10, step="5s", queries=QUERIES)
    rtt, load = summary["queries"]
    assert rtt["rows"] == 1
    assert "bad query" in load["error"]


def test_an_unreachable_prometheus_is_recorded_per_query(tmp_path):
    summary = export_ranges(tmp_path, url="http://127.0.0.1:1", start=0, end=10, step="5s", queries=QUERIES)
    assert all(query["error"] for query in summary["queries"])
    assert rows(tmp_path) == []


def test_an_empty_range_is_refused(tmp_path):
    with pytest.raises(ValueError, match="end must be greater than its start"):
        export_ranges(tmp_path, url="http://x", start=10, end=10, step="5s", queries=QUERIES)


def test_only_http_urls_are_queried():
    with pytest.raises(ValueError, match="Expected an HTTP"):
        http_json("file:///etc/passwd")


def test_the_shipped_queries_load():
    queries = load_queries(None)
    assert queries == load_queries(DEFAULT_QUERIES_FILE)
    assert all(query["name"] and query["query"] for query in queries)


def test_a_query_without_a_name_is_refused(tmp_path):
    path = tmp_path / "queries.json"
    path.write_text(json.dumps([{"query": "up"}]))
    with pytest.raises(ValueError, match="Invalid query item"):
        load_queries(path)


# Splitting by window


def timeline(directory, sections):
    """Write the timeline of a campaign 0-100 with the given (name, start, end) sections."""
    events = [{"run_id": "r", "level": "campaign", "phase": "start", "name": "exp", "epoch": 0}]
    for name, start, end in sections:
        events += [
            {"run_id": "r", "level": "section", "phase": "start", "name": name, "epoch": start},
            {"run_id": "r", "level": "section", "phase": "end", "name": name, "epoch": end, "status": "completed"},
        ]
    events.append({"run_id": "r", "level": "campaign", "phase": "end", "name": "exp", "epoch": 100})
    path = directory / "timeline_events.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    finalize_timeline(path, directory, run_id="r", timezone="UTC")


def test_samples_are_copied_into_every_window_they_fall_in(tmp_path, http):
    http.routes["/api/v1/query_range"] = lambda q: matrix(({}, [(5, "1"), (15, "2"), (50, "3")]))
    export_ranges(tmp_path, url=http.url, start=0, end=100, step="5s", queries=QUERIES[:1])
    timeline(tmp_path, [("ping", 10, 20)])
    split_windows(tmp_path)
    summary = json.loads((tmp_path / "by_window" / "split_summary.json").read_text())
    assert {w["name"]: w["rows"] for w in summary["windows"]} == {"exp": 3, "ping": 1}
    ping = next(w for w in summary["windows"] if w["name"] == "ping")
    with gzip.open(ping["prometheus_csv"], "rt") as f:
        assert [row["value"] for row in csv.DictReader(f)] == ["2"]


def test_splitting_without_an_export_records_why(tmp_path):
    split_windows(tmp_path)
    summary = json.loads((tmp_path / "by_window" / "split_summary.json").read_text())
    assert summary["error"].startswith("missing Prometheus CSV")


def test_splitting_without_windows_records_why(tmp_path):
    (tmp_path / "prometheus_timeseries.csv").write_text("timestamp,value\n1,1\n")
    (tmp_path / "timeline_summary.json").write_text(json.dumps({"windows": []}))
    split_windows(tmp_path)
    assert json.loads((tmp_path / "by_window" / "split_summary.json").read_text())["error"] == "no matching windows"


def test_an_empty_export_is_left_unsplit(tmp_path):
    (tmp_path / "prometheus_timeseries.csv").write_text("")
    timeline(tmp_path, [])
    split_windows(tmp_path)
    assert not (tmp_path / "by_window" / "split_summary.json").exists()
