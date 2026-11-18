"""Experiment runs without a cluster: argument checks and local artifacts."""

import asyncio
import json

import pytest

from nador.artifacts import Collector, ExperimentRun, LokiExport, RunOptions


def test_run_defaults(tmp_path):
    run = ExperimentRun(tmp_path / "run", name="x")
    assert run.name == "x"
    assert run.options.sampler_args() == [
        *("--interval-seconds", "2", "--timeout-seconds", "5", "--max-seconds", "7200"),
        *("--limit", "5000", "--service", "ue-mapper-api", "--service-port", "8080"),
    ]
    assert run.collectors == []  # no monitoring manager, Prometheus, mapper or Loki URL: timeline only


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mapper_url": "ftp://mapper"},
        {"mapper_url": "http://mapper/?q=1"},
        {"timeout_seconds": 61},
        {"interval_seconds": True},
        {"interval_seconds": float("inf")},
        {"limit": 0},
        {"service_port": 70000},
        {"loki_limit": 100_001},
        {"setup_hosts": [""]},
        {"setup_hosts": ["node1"]},  # needs a monitoring manager that knows node1
        {"colect_loki": True},  # misspelled option
    ],
)
def test_bad_options_are_rejected(tmp_path, kwargs):
    with pytest.raises(ValueError):  # noqa: PT011 - pydantic and custom messages vary
        ExperimentRun(tmp_path / "run", **kwargs)


def test_loki_url_enables_log_export():
    assert RunOptions(loki_url="http://loki:3100").loki_url
    run = ExperimentRun("unused", loki_url="http://loki:3100")
    assert [type(collector) for collector in run.collectors] == [LokiExport]


def test_run_without_prometheus_writes_timeline(tmp_path):
    async def experiment():
        async with ExperimentRun(tmp_path / "run", name="exp", timezone="UTC") as run:
            with run.section("work"):
                run.save_json("result.json", {"ok": True})
        return run

    run = asyncio.run(experiment())
    metadata = json.loads((run.directory / "experiment_metadata.json").read_text())
    assert metadata["status"] == "completed"
    assert metadata["collection_errors"] == []
    assert (run.directory / "timeline_summary.csv").exists()
    assert json.loads((run.directory / "result.json").read_text()) == {"ok": True}


def test_failed_section_marks_run_failed(tmp_path):
    async def experiment():
        async with ExperimentRun(tmp_path / "run", timezone="UTC") as run:
            with run.section("boom"):
                raise RuntimeError("workload failed")

    with pytest.raises(RuntimeError, match="workload failed"):
        asyncio.run(experiment())
    metadata = json.loads((tmp_path / "run" / "experiment_metadata.json").read_text())
    assert metadata["status"] == "failed"
    assert metadata["error"] == "workload failed"


@pytest.mark.parametrize(
    ("url", "safe"),
    [
        ("https://controller.example:8787", True),
        ("http://127.0.0.1:8787", True),
        ("http://localhost:8787", True),
        ("http://controller.example:8787", False),
        ("https://user:pw@controller.example", False),
        ("https://controller.example/?x=1", False),
        ("ftp://127.0.0.1", False),
    ],
)
def test_controller_cli_only_sends_tokens_safely(url, safe):
    from urllib.parse import urlsplit

    from nador.probe_control_cli import _is_safe_endpoint

    assert _is_safe_endpoint(urlsplit(url)) is safe


class Recorder(Collector):
    """Records the order of lifecycle calls; can be told to fail a step."""

    def __init__(self, label, calls, fail=None):
        self.label = label
        self.calls = calls
        self.fail = fail

    async def _step(self, step):
        self.calls.append(f"{self.label}.{step}")
        if step == self.fail:
            raise RuntimeError("boom")

    async def start(self, run):
        await self._step("start")

    async def stop(self, run):
        await self._step("stop")

    async def collect(self, run, start, end):
        assert (run.directory / "timeline_summary.json").exists()
        assert start < end
        await self._step("collect")


def test_collectors_run_in_order_and_failures_are_isolated(tmp_path):
    calls = []
    collectors = [Recorder("a", calls, fail="start"), Recorder("b", calls)]

    async def experiment():
        async with ExperimentRun(tmp_path / "run", timezone="UTC", collectors=collectors) as run:
            calls.append("workload")
        return run

    with pytest.raises(RuntimeError, match="Artifact collection incomplete"):
        asyncio.run(experiment())
    assert calls == ["a.start", "b.start", "workload", "b.stop", "a.stop", "a.collect", "b.collect"]
    metadata = json.loads((tmp_path / "run" / "experiment_metadata.json").read_text())
    assert metadata["status"] == "completed"
    assert metadata["collection_errors"] == ["a start: boom"]


def test_cancelled_run_still_stops_collectors(tmp_path):
    calls = []

    async def experiment():
        async with ExperimentRun(tmp_path / "run", timezone="UTC", collectors=[Recorder("a", calls)]):
            await asyncio.sleep(3600)

    async def main():
        task = asyncio.create_task(experiment())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    assert calls == ["a.start", "a.stop", "a.collect"]
    assert json.loads((tmp_path / "run" / "experiment_metadata.json").read_text())["status"] == "cancelled"


def test_local_ue_sampler_runs_as_a_subprocess(tmp_path):
    import http.server
    import threading

    class Mapper(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps({"ues": [{"imsi": "001010000000001"}], "count": 1, "limit": 5000}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Mapper)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:

        async def experiment():
            url = f"http://127.0.0.1:{server.server_port}"
            async with ExperimentRun(tmp_path / "run", mapper_url=url, interval_seconds=0.2, timezone="UTC"):
                await asyncio.sleep(0.6)

        asyncio.run(experiment())
    finally:
        server.shutdown()

    output = tmp_path / "run" / "ue_context"
    assert json.loads((output / "collection_status.json").read_text())["status"] == "completed"
    summary = json.loads((output / "summary.json").read_text())
    assert summary["samples"] >= 2
    assert summary["errors"] == 0
    assert summary["stop_reason"] == "requested"
