"""The built-in collectors of an experiment run: metrics from Prometheus and logs from Loki."""

import json
from contextlib import asynccontextmanager

import pytest
from support import manager, matrix, options, profile

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


# With a monitoring manager, Prometheus and Loki are reached through it


def through(url):
    @asynccontextmanager
    async def connection(monitoring):
        yield url

    return connection


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
