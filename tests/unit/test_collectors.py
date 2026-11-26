"""The built-in collectors of an experiment run: metrics from Prometheus and logs from Loki."""

import json
from contextlib import asynccontextmanager

import pytest
from support import manager, matrix, network, options, profile

from nador.artifacts import ExperimentRun, collectors

NO_HOSTS = {"collect_setup": False, "collect_ue_context": False}


def streams(*lines):
    values = [[str(i), line] for i, line in enumerate(lines)]
    return lambda q: {
        "status": "success",
        "data": {"resultType": "streams", "result": [{"stream": {}, "values": values}]},
    }


async def experiment(directory, **kwargs):
    async with ExperimentRun(directory, timezone="UTC", **kwargs) as run:
        with run.section("work"):
            pass
    return run


def metadata(directory):
    return json.loads((directory / "experiment_metadata.json").read_text())


@pytest.fixture
def queries(tmp_path):
    path = tmp_path / "queries.json"
    path.write_text(json.dumps([{"name": "rtt", "query": "rtt"}]))
    return path


async def test_metrics_and_logs_are_saved_with_the_run(tmp_path, http, queries):
    http.routes["/api/v1/query_range"] = lambda q: matrix(({}, [(float(q["start"]), "1")]))
    http.routes["/loki/api/v1/query_range"] = streams("gnb started")
    run_dir = tmp_path / "run"
    await experiment(run_dir, prometheus_url=http.url, queries_file=queries, loki_url=http.url)
    assert metadata(run_dir)["collection_errors"] == []
    assert (run_dir / "prometheus_timeseries.csv").read_text().count("\n") == 2
    assert (run_dir / "by_window" / "split_summary.json").exists()
    assert json.loads((run_dir / "logs" / "loki.jsonl").read_text())["line"] == "gnb started"


async def test_a_run_whose_queries_return_nothing_is_incomplete(tmp_path, http, queries):
    http.routes["/api/v1/query_range"] = lambda q: {"status": "error", "error": "bad"}
    with pytest.raises(RuntimeError, match="Artifact collection incomplete"):
        await experiment(tmp_path / "run", prometheus_url=http.url, queries_file=queries)
    errors = metadata(tmp_path / "run")["collection_errors"]
    assert errors[0].startswith("rtt: ")
    assert errors[1] == "prometheus: returned no samples"


async def test_a_possibly_truncated_log_export_is_flagged(tmp_path, http):
    http.routes["/loki/api/v1/query_range"] = streams("a", "b")
    with pytest.raises(RuntimeError, match="Artifact collection incomplete"):
        await experiment(tmp_path / "run", loki_url=http.url, loki_limit=2, loki_max_queries=1)
    (error,) = metadata(tmp_path / "run")["collection_errors"]
    assert error == "loki: query budget exhausted (1/1); possible truncation; log export may be incomplete"


# Large log exports: filtered in Loki, announced before they start


def line_count(lines):
    return lambda q: {"status": "success", "data": {"resultType": "vector", "result": [{"value": [0, str(lines)]}]}}


async def test_ocudu_phy_detail_is_filtered_out_by_loki_by_default(tmp_path, http):
    http.routes["/loki/api/v1/query_range"] = streams("kept")
    await experiment(tmp_path / "run", loki_url=http.url)
    (query,) = {q["query"] for path, q in http.requests if path == "/loki/api/v1/query_range"}
    assert query == '{namespace=~".+"} !~ `\\[PHY *\\] \\[[DI]\\]`'
    summary = json.loads((tmp_path / "run" / "logs" / "summary.json").read_text())
    assert summary["dropped_lines_matching"] == [r"\[PHY *\] \[[DI]\]"]
    assert summary["seconds"] >= 0


async def test_every_line_is_kept_with_an_empty_drop_list(tmp_path, http):
    http.routes["/loki/api/v1/query_range"] = streams("kept")
    await experiment(tmp_path / "run", loki_url=http.url, loki_drop=[], loki_selector='{namespace="ran"}')
    assert {q["query"] for path, q in http.requests if path == "/loki/api/v1/query_range"} == {'{namespace="ran"}'}


async def test_a_large_export_is_announced_with_how_to_shorten_it(tmp_path, http):
    http.routes["/loki/api/v1/query"] = line_count(420_000)
    http.routes["/loki/api/v1/query_range"] = streams("line")
    with pytest.warns(RuntimeWarning, match=r"about 420,000 log lines for this run, 84\+ queries.*loki_drop"):
        await experiment(tmp_path / "run", loki_url=http.url)
    (count,) = [q["query"] for path, q in http.requests if path == "/loki/api/v1/query"]
    assert count.startswith('sum(count_over_time({namespace=~".+"} !~ `')


@pytest.mark.parametrize("options", [{"loki_warn_lines": 500_000}, {"loki_warn_lines": 0}])
async def test_no_warning_below_the_threshold_or_when_turned_off(tmp_path, http, options):
    http.routes["/loki/api/v1/query"] = line_count(420_000)
    http.routes["/loki/api/v1/query_range"] = streams("line")
    await experiment(tmp_path / "run", loki_url=http.url, **options)  # filterwarnings=error: a warning would fail
    asked = [path for path, _ in http.requests if path == "/loki/api/v1/query"]
    assert asked == ([] if options["loki_warn_lines"] == 0 else ["/loki/api/v1/query"])


async def test_an_unknown_line_count_does_not_stop_the_export(tmp_path, http):
    http.routes["/loki/api/v1/query"] = lambda q: (500, {"error": "too many outstanding requests"})
    http.routes["/loki/api/v1/query_range"] = streams("line")
    await experiment(tmp_path / "run", loki_url=http.url)
    assert (tmp_path / "run" / "logs" / "loki.jsonl").read_text()


def test_a_line_pattern_loki_cannot_quote_is_refused():
    with pytest.raises(ValueError, match="without backquotes"):
        ExperimentRun("unused", loki_url="http://loki:3100", loki_drop=["a`b"])


# With a monitoring manager, Prometheus and Loki are reached through it


def through(url):
    @asynccontextmanager
    async def connection(monitoring):
        yield url

    return connection


CELLS = "max by (namespace, service) (oai_gnb_cells)"


def cells(count):
    """Prometheus answering an OAI pod with ``count`` cells, and one sample for every other query."""
    du = {"namespace": "ran", "service": "metrics-ran-du1"}
    return lambda q: (
        matrix((du, [(float(q["start"]), str(count))]))
        if q["query"] == CELLS
        else matrix(({}, [(float(q["start"]), "1")]))
    )


async def test_a_run_warns_when_an_oai_pod_served_several_cells(tmp_path, http, queries):
    http.routes["/api/v1/query_range"] = cells(2)
    with pytest.warns(RuntimeWarning, match=r"several cells: ran/metrics-ran-du1\. Their L1 metrics"):
        await experiment(tmp_path / "run", prometheus_url=http.url, queries_file=queries)
    (warning,) = metadata(tmp_path / "run")["warnings"]
    assert warning.startswith("prometheus: OAI gNB/DU pods with several cells: ran/metrics-ran-du1.")
    assert metadata(tmp_path / "run")["status"] == "completed"  # a caveat, not a failed collection


async def test_one_cell_per_pod_gives_no_warning(tmp_path, http, queries):
    http.routes["/api/v1/query_range"] = cells(1)
    await experiment(tmp_path / "run", prometheus_url=http.url, queries_file=queries)  # filterwarnings=error
    assert "warnings" not in metadata(tmp_path / "run")


async def test_the_cell_check_needs_a_ran_that_may_run_the_oai_parser(tmp_path, http, queries, monkeypatch):
    http.routes["/api/v1/query_range"] = cells(2)
    monkeypatch.setattr(collectors, "prometheus_connection", through(http.url))
    ocudu = manager(profile(metrics={"ran_kpis": options("ran_kpis")}), network(ran="ocudu"))
    await experiment(tmp_path / "run", monitoring=ocudu, queries_file=queries, **NO_HOSTS)
    assert CELLS not in [query["query"] for _, query in http.requests]


async def test_the_monitoring_manager_opens_prometheus_and_adds_host_probe_series(tmp_path, http, queries, monkeypatch):
    http.routes["/api/v1/query_range"] = lambda q: matrix(({}, [(float(q["start"]), "1")]))
    monkeypatch.setattr(collectors, "prometheus_connection", through(http.url))
    monitoring = manager(profile(metrics={"scheduler_wait": options("scheduler_wait")}))
    await experiment(tmp_path / "run", monitoring=monitoring, queries_file=queries, **NO_HOSTS)
    assert [query["query"] for _, query in http.requests] == ["rtt", '{__name__=~"nador_.*"}']


async def test_logs_are_exported_when_the_profile_keeps_them(tmp_path, http, monkeypatch):
    http.routes["/api/v1/query_range"] = lambda q: matrix(({}, [(float(q["start"]), "1")]))
    http.routes["/loki/api/v1/query_range"] = streams("line")
    monkeypatch.setattr(collectors, "prometheus_connection", through(http.url))
    monkeypatch.setattr(collectors, "loki_connection", through(http.url))
    await experiment(tmp_path / "run", monitoring=manager(profile(metrics={"core_logs": True})), **NO_HOSTS)
    assert (tmp_path / "run" / "logs" / "loki.jsonl").exists()


async def test_an_unreachable_service_is_recorded(tmp_path, monkeypatch):
    @asynccontextmanager
    async def refused(monitoring):
        raise RuntimeError("Prometheus SSH tunnel failed: Connection refused")
        yield  # pragma: no cover

    monkeypatch.setattr(collectors, "prometheus_connection", refused)
    with pytest.raises(RuntimeError, match="Artifact collection incomplete"):
        await experiment(tmp_path / "run", monitoring=manager(profile()), **NO_HOSTS)
    (error,) = metadata(tmp_path / "run")["collection_errors"]
    assert error == "prometheus access: Prometheus SSH tunnel failed: Connection refused"


# Using a run correctly


def test_monitoring_and_a_prometheus_url_are_exclusive(tmp_path):
    with pytest.raises(ValueError, match="Supply monitoring or prometheus_url, not both"):
        ExperimentRun(tmp_path, monitoring=manager(profile()), prometheus_url="http://p")


def test_setup_hosts_must_be_nodes_of_the_deployment(tmp_path):
    with pytest.raises(ValueError, match="setup_hosts must refer to nodes"):
        ExperimentRun(tmp_path, monitoring=manager(profile()), setup_hosts=["node9"])


async def test_sections_and_artifacts_belong_inside_the_run(tmp_path):
    run = ExperimentRun(tmp_path / "run")
    with pytest.raises(RuntimeError, match="inside async with"):
        run.save_json("x.json", {})
    with pytest.raises(RuntimeError, match="inside async with"), run.section("early"):
        pass
    async with run:
        with pytest.raises(ValueError, match="cannot be empty"), run.section(" "):
            pass
        with run.section("once"):
            pass
        with pytest.raises(ValueError, match="Duplicate section 'once'"), run.section("once"):
            pass
    with pytest.raises(RuntimeError, match="cannot be entered twice"):
        await run.__aenter__()
