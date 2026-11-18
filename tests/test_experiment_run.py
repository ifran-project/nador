"""Experiment runs without a cluster: argument checks and local artifacts."""

import asyncio
import json

import pytest

from nador.artifacts import ExperimentRun, ObservabilityOptions, ObservabilityRun


def test_observability_defaults(tmp_path):
    run = ObservabilityRun(tmp_path / "run", name="x")
    assert run.name == "x"
    assert run.options == {
        "interval_seconds": 2,
        "timeout_seconds": 5,
        "max_seconds": 7200,
        "limit": 5000,
        "service": "ue-mapper-api",
        "service_port": 8080,
    }
    assert run.queries_file.name == "observability_queries.json"


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
    ],
)
def test_observability_rejects_bad_options(tmp_path, kwargs):
    with pytest.raises(ValueError):  # noqa: PT011 - pydantic and custom messages vary
        ObservabilityRun(tmp_path / "run", **kwargs)


def test_unknown_arguments_are_rejected(tmp_path):
    with pytest.raises(TypeError):
        ObservabilityRun(tmp_path / "run", colect_loki=True)


def test_loki_url_enables_loki_export():
    assert ObservabilityOptions(loki_url="http://loki:3100").loki_url
    run = ObservabilityRun("unused", loki_url="http://loki:3100")
    assert run.collect_loki


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
