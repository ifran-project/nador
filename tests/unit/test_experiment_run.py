"""Experiment runs without a cluster: options, sections, collectors and the files they leave."""

import asyncio
import json

import pytest

from nador.artifacts import Collector, ExperimentRun, LokiExport, RunOptions


def test_defaults(tmp_path):
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
def test_bad_options_are_refused(tmp_path, kwargs):
    with pytest.raises(ValueError):  # noqa: PT011 - pydantic and custom messages vary
        ExperimentRun(tmp_path / "run", **kwargs)


def test_a_loki_url_turns_on_log_export():
    assert RunOptions(loki_url="http://loki:3100").loki_url
    run = ExperimentRun("unused", loki_url="http://loki:3100")
    assert [type(collector) for collector in run.collectors] == [LokiExport]


async def test_a_run_without_prometheus_still_writes_its_timeline(tmp_path):
    async with ExperimentRun(tmp_path / "run", name="exp", timezone="UTC") as run:
        with run.section("work"):
            run.save_json("result.json", {"ok": True})

    metadata = json.loads((run.directory / "experiment_metadata.json").read_text())
    assert metadata["status"] == "completed"
    assert metadata["collection_errors"] == []
    assert (run.directory / "timeline_summary.csv").exists()
    assert json.loads((run.directory / "result.json").read_text()) == {"ok": True}


async def test_a_failed_section_marks_the_run_failed(tmp_path):
    async def experiment():
        async with ExperimentRun(tmp_path / "run", timezone="UTC") as run:
            with run.section("boom"):
                raise RuntimeError("workload failed")

    with pytest.raises(RuntimeError, match="workload failed"):
        await experiment()

    metadata = json.loads((tmp_path / "run" / "experiment_metadata.json").read_text())
    assert metadata["status"] == "failed"
    assert metadata["error"] == "workload failed"


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


async def test_collectors_run_in_order_and_one_failure_does_not_stop_the_others(tmp_path):
    calls = []
    collectors = [Recorder("a", calls, fail="start"), Recorder("b", calls)]
    with pytest.raises(RuntimeError, match="Artifact collection incomplete"):
        async with ExperimentRun(tmp_path / "run", timezone="UTC", collectors=collectors):
            calls.append("workload")

    assert calls == ["a.start", "b.start", "workload", "b.stop", "a.stop", "a.collect", "b.collect"]
    metadata = json.loads((tmp_path / "run" / "experiment_metadata.json").read_text())
    assert metadata["status"] == "completed"
    assert metadata["collection_errors"] == ["a start: boom"]


async def test_a_cancelled_run_still_stops_and_collects(tmp_path):
    calls = []

    async def experiment():
        async with ExperimentRun(tmp_path / "run", timezone="UTC", collectors=[Recorder("a", calls)]):
            await asyncio.sleep(3600)

    task = asyncio.create_task(experiment())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["a.start", "a.stop", "a.collect"]
    assert json.loads((tmp_path / "run" / "experiment_metadata.json").read_text())["status"] == "cancelled"
