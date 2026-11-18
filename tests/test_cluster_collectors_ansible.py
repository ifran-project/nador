"""Run the cluster collectors' Ansible role for real, against this machine.

Ansible connects locally and a fake kubectl answers the UE inventory and
cluster queries, so the snapshot, sampler start/stop, fetch and cleanup
steps all execute without a cluster.
"""

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from nador.artifacts import ExperimentRun

FAKE_KUBECTL = """#!/usr/bin/env python3
import json, sys
if "--raw" in sys.argv:
    print(json.dumps({"ues": [{"imsi": "001010000000001", "ue_ip": "10.45.0.2"}], "count": 1, "limit": 5000}))
else:
    print(json.dumps({"items": []}))
"""

pytestmark = pytest.mark.skipif(
    shutil.which("ansible-playbook", path=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]) is None,
    reason="ansible-playbook is not installed",
)


def local_monitoring(runner_path: Path) -> SimpleNamespace:
    """The parts of a MonitoringManagement that ObservabilityRun reads, pointing at localhost."""
    node = SimpleNamespace(
        name="local",
        ssh_info=SimpleNamespace(
            hostname="127.0.0.1",
            port=22,
            user="root",
            private_key_file=None,
            password=None,
            sudo_password=None,
            common_args=None,
        ),
        provisioning_params={
            "ansible_connection": "local",
            "ansible_become": "false",
            "ansible_python_interpreter": sys.executable,
        },
    )
    model = SimpleNamespace(
        nodes={"local": node},
        params=SimpleNamespace(
            k8s=SimpleNamespace(master_node="local"),
            ansible=SimpleNamespace(runner_path=str(runner_path), roles_paths=[]),
        ),
        # No Prometheus or Loki here: the run records that collection as incomplete.
        monitoring=SimpleNamespace(
            kubeconfig="/dev/null", namespace="monitoring", needs_session_stack=True, metrics=SimpleNamespace(logs=None)
        ),
    )
    return SimpleNamespace(deployment_model=model)


def test_remote_context_collection(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(FAKE_KUBECTL)
    kubectl.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}")

    async def experiment():
        run = ExperimentRun(
            tmp_path / "run", monitoring=local_monitoring(tmp_path / "runner"), interval_seconds=0.5, timezone="UTC"
        )
        async with run:
            with run.section("work"):
                await asyncio.sleep(1.5)

    # The fake manager has no Prometheus, so collection is reported as incomplete.
    with pytest.raises(RuntimeError, match="Artifact collection incomplete"):
        asyncio.run(experiment())

    run_dir = tmp_path / "run"
    errors = json.loads((run_dir / "experiment_metadata.json").read_text())["collection_errors"]
    assert not [e for e in errors if e.startswith(("ue_context", "setup before cluster", "setup after cluster"))]
    assert json.loads((run_dir / "ue_context" / "collection_status.json").read_text())["status"] == "completed"
    summary = json.loads((run_dir / "ue_context" / "summary.json").read_text())
    assert summary["samples"] >= 1
    assert summary["stop_reason"] == "requested"
    for phase in ("before", "after"):
        hosts = json.loads((run_dir / "setup" / phase / "hosts.json").read_text())
        assert hosts[0]["host"] == "local"
        assert hosts[0]["mode"] == "host"
        assert json.loads((run_dir / "setup" / phase / "cluster.json").read_text())["mode"] == "cluster"
    assert not list(Path("/tmp").glob(f"nador-ml-{summary['run_id']}"))  # noqa: S108
