"""The metrics check command: which queries return data, which targets are down, whether Loki ingests."""

import json
import sys

import pytest
from support import matrix

from nador.artifacts import metrics_check_cli as check

UP = {"labels": {"job": "prometheus"}, "health": "up", "lastError": "", "lastScrape": "now"}


def targets(*items):
    return lambda q: {"status": "success", "data": {"activeTargets": list(items)}}


def streams(count):
    values = [["1", "line"]] * count
    return lambda q: {"status": "success", "data": {"resultType": "streams", "result": [{"values": values}]}}


def run(monkeypatch, tmp_path, *args):
    """Run the command as a user would; return its exit code and report."""
    out = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["metrics-check", *args, "--out", str(out)])
    code = check.main()
    return code, json.loads(out.read_text())


@pytest.fixture
def queries(tmp_path):
    path = tmp_path / "queries.json"
    path.write_text(json.dumps([{"name": "rtt", "query": "rtt"}, {"name": "load", "query": "load"}]))
    return str(path)


def test_a_healthy_deployment_exits_zero(monkeypatch, tmp_path, http, queries):
    http.routes["/api/v1/targets"] = targets(UP)
    http.routes["/api/v1/query_range"] = lambda q: matrix(({"pod": "a"}, [(1, "1")]))
    http.routes["/loki/api/v1/query_range"] = streams(1)
    code, report = run(
        monkeypatch, tmp_path, "--prometheus-url", http.url, "--queries-json", queries, "--loki-url", http.url
    )
    assert code == 0
    assert report["counts"] == {"available": 2}
    assert report["loki"] == {"status": "available", "selector": '{namespace=~".+"}'}
    assert report["unhealthy_targets"] == []


def test_a_down_target_makes_it_unhealthy(monkeypatch, tmp_path, http, queries):
    http.routes["/api/v1/targets"] = targets(UP, {"labels": {"job": "upf"}, "health": "down", "lastError": "refused"})
    http.routes["/api/v1/query_range"] = lambda q: matrix(({}, [(1, "1")]))
    code, report = run(monkeypatch, tmp_path, "--prometheus-url", http.url, "--queries-json", queries)
    assert code == 2
    assert [t["labels"]["job"] for t in report["unhealthy_targets"]] == ["upf"]


def test_empty_queries_are_reported_but_not_failures(monkeypatch, tmp_path, http, queries):
    http.routes["/api/v1/targets"] = targets(UP)
    http.routes["/api/v1/query_range"] = lambda q: matrix()
    code, report = run(monkeypatch, tmp_path, "--prometheus-url", http.url, "--queries-json", queries)
    assert code == 0
    assert report["counts"] == {"empty": 2}


def test_a_required_expectation_without_data_fails(monkeypatch, tmp_path, http, queries):
    expectations = tmp_path / "expect.json"
    expectations.write_text(json.dumps([{"name": "rtt arrives", "query": "rtt"}]))
    http.routes["/api/v1/targets"] = targets(UP)
    http.routes["/api/v1/query_range"] = lambda q: matrix() if q["query"] == "rtt" else matrix(({}, [(1, "1")]))
    code, report = run(
        monkeypatch,
        tmp_path,
        *("--prometheus-url", http.url, "--queries-json", queries, "--expectations-json", str(expectations)),
    )
    assert code == 2
    assert report["expectation_counts"] == {"empty": 1}


def test_an_unreachable_prometheus_and_loki_are_errors(monkeypatch, tmp_path, queries):
    dead = "http://127.0.0.1:1"
    code, report = run(monkeypatch, tmp_path, "--prometheus-url", dead, "--queries-json", queries, "--loki-url", dead)
    assert code == 2
    assert report["scrape_targets"]["status"] == "error"
    assert report["loki"]["status"] == "error"
    assert {item["status"] for item in report["queries"]} == {"error"}


def test_a_lookback_too_short_for_rate_is_refused(monkeypatch, tmp_path, queries):
    with pytest.raises(SystemExit):
        run(monkeypatch, tmp_path, "--prometheus-url", "http://x", "--lookback-seconds", "60")


def test_an_unreadable_expectations_file_is_refused(monkeypatch, tmp_path, queries):
    with pytest.raises(SystemExit):
        run(monkeypatch, tmp_path, "--prometheus-url", "http://x", "--expectations-json", str(tmp_path / "none"))


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "must contain a JSON list"),
        (["rtt"], "must be an object"),
        ([{"name": " ", "query": "q"}], "needs a non-empty name"),
        ([{"name": "a", "query": ""}], "needs a non-empty query"),
        ([{"name": "a", "query": "q"}, {"name": "a", "query": "r"}], "Duplicate readiness expectation"),
    ],
)
def test_expectation_files_are_checked(tmp_path, payload, message):
    path = tmp_path / "expect.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match=message):
        check.load_expectations(str(path))


@pytest.mark.parametrize(
    ("values", "status"),
    [
        (["1", "2"], "available"),
        ([], "empty"),
        (["1", "NaN"], "partial"),
        (["+Inf"], "non_finite"),
    ],
)
def test_query_results_are_classified(values, status):
    payload = matrix(({}, [(i, value) for i, value in enumerate(values)]))
    assert check.summarize_query(payload)["status"] == status


def test_a_failed_or_instant_query_is_an_error():
    assert check.summarize_query({"status": "error", "error": "boom"}) == {"status": "error", "error": "boom"}
    vector = {"status": "success", "data": {"resultType": "vector", "result": []}}
    assert check.summarize_query(vector)["status"] == "error"


def test_loki_must_answer_with_streams(http):
    http.routes["/loki/api/v1/query_range"] = lambda q: {"status": "success", "data": {"resultType": "matrix"}}
    with pytest.raises(ValueError, match="Expected a Loki log-stream result"):
        check.check_loki(http.url, "{}", 0, 1)
    http.routes["/loki/api/v1/query_range"] = lambda q: {"status": "error", "error": "parse error"}
    with pytest.raises(ValueError, match="parse error"):
        check.check_loki(http.url, "{}", 0, 1)
    http.routes["/loki/api/v1/query_range"] = streams(0)
    assert check.check_loki(http.url, "{}", 0, 1)["status"] == "empty"


def test_prometheus_must_list_its_targets(http):
    http.routes["/api/v1/targets"] = lambda q: {"status": "error", "error": "forbidden"}
    with pytest.raises(ValueError, match="forbidden"):
        check.check_targets(http.url)
    http.routes["/api/v1/targets"] = targets()
    assert check.check_targets(http.url)["status"] == "empty"
