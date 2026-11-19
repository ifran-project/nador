"""Collectors that act on the cluster hosts through the ``experiment/context`` Ansible role.

``SetupSnapshot`` records host and cluster state before and after the workload, and
``UEContext`` samples the UE mapper's inventory while it runs. With a ``mapper_url``
the sampler runs on this machine instead.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ifran.utils.ansible import get_ansible_inventory_line, run_ansible_role

from nador.planner import ASSETS

from .collectors import Collector, RunOptions

if TYPE_CHECKING:
    from nador.deployment import MonitoringManagement

    from .experiment_run import ExperimentRun

HERE = Path(__file__).resolve().parent
SNAPSHOT_SCRIPT = HERE / "setup_snapshot.py"
SAMPLER_SCRIPT = HERE / "ue_sampler.py"


class ClusterHosts:
    """Runs the experiment/context Ansible role on the hosts of a deployment."""

    def __init__(self, monitoring: MonitoringManagement, namespace: str | None) -> None:
        model = monitoring.deployment_model
        control = model.params.k8s.master_node
        if not control or control not in model.nodes:
            raise ValueError("Remote collection requires a control node in monitoring.deployment_model.nodes")
        self.model = model
        self.control = control
        self.kubeconfig = model.monitoring.kubeconfig
        self.namespace = namespace or model.monitoring.namespace

    def reachable(self) -> list[str]:
        """The control node plus every node with an SSH address."""
        return [
            name
            for name, node in self.model.nodes.items()
            if name == self.control or getattr(getattr(node, "ssh_info", None), "hostname", None)
        ]

    async def run(self, run: ExperimentRun, action: str, inputs: dict, host: str | None = None, outputs=None) -> dict:
        """Run one role action; ``outputs`` names the values to return."""
        host = host or self.control
        result = await run_ansible_role(
            role="experiment/context",
            hosts=host,
            inventory=get_ansible_inventory_line(host, self.model.nodes[host]),
            become=True,
            inputs={"context_action": action, **inputs},
            output_keys=outputs,
            runner_path=str(Path(self.model.params.ansible.runner_path) / "experiments" / run.run_id / action),
            roles_paths=[str(ASSETS / "roles"), *self.model.params.ansible.roles_paths],
            quiet=True,
        )
        return result or {}


class SetupSnapshot(Collector):
    """Host and cluster state, before and after the workload, in setup/<phase>/."""

    label = "setup"

    def __init__(self, hosts: ClusterHosts, names: list[str] | None) -> None:
        self.hosts = hosts
        self.names = names

    async def start(self, run: ExperimentRun) -> None:
        """Snapshot before the workload."""
        await self._snapshot(run, "before")

    async def stop(self, run: ExperimentRun) -> None:
        """Snapshot after the workload."""
        await self._snapshot(run, "after")

    async def _snapshot(self, run: ExperimentRun, phase: str) -> None:
        results = []
        for host in self.names or self.hosts.reachable():
            try:
                results.append({"host": host, **await self._read(run, host, ["--mode", "host"])})
            except Exception as exc:
                run.error(f"setup {phase} host {host}", exc)
                results.append({"host": host, "status": "error", "error": str(exc)})
        run.save_json(f"setup/{phase}/hosts.json", results)
        try:
            args = ["--mode", "cluster", "--kubeconfig", self.hosts.kubeconfig]
            cluster = await self._read(run, self.hosts.control, args)
        except Exception as exc:
            run.error(f"setup {phase} cluster", exc)
            cluster = {"status": "error", "error": str(exc)}
        run.save_json(f"setup/{phase}/cluster.json", cluster)

    async def _read(self, run: ExperimentRun, host: str, args: list[str]) -> dict:
        inputs = {"snapshot_script": str(SNAPSHOT_SCRIPT), "snapshot_args": args}
        result = await self.hosts.run(run, "snapshot", inputs, host=host, outputs=["snapshot"])
        snapshot = json.loads(result["snapshot"])
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("data"), dict):
            raise ValueError("Invalid setup snapshot")
        for name, value in snapshot["data"].items():
            if isinstance(value, dict) and value.get("status") == "error":
                run.error(f"setup {host} {name}", value.get("error", "command failed"))
        return snapshot


class UEContext(Collector):
    """The UE inventory sampled while the workload runs, in ue_context/.

    The sampler runs on the control node and reads the mapper through the Kubernetes
    service proxy, or on this machine when ``mapper_url`` is set. Either way it is
    stopped with a STOP file and reports through ready.json and summary.json.
    """

    label = "ue_context"

    def __init__(self, options: RunOptions, hosts: ClusterHosts | None) -> None:
        self.options = options
        self.hosts = hosts
        self.process: asyncio.subprocess.Process | None = None
        self.remote_dir: str | None = None
        self.job_id = ""

    @property
    def _ready_timeout(self) -> int:
        return self.options.timeout_seconds + 15

    async def start(self, run: ExperimentRun) -> None:
        """Start sampling and wait for the first sample."""
        if self.options.mapper_url:
            await self._start_local(run)
        else:
            await self._start_remote(run)

    async def _start_local(self, run: ExperimentRun) -> None:
        output = run.directory / "ue_context"
        output.mkdir(parents=True, exist_ok=True)
        self.process = process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(SAMPLER_SCRIPT),
            *("--out-dir", str(output), "--stop-file", str(output / "STOP"), "--run-id", run.run_id),
            *("--url", str(self.options.mapper_url), *self.options.sampler_args()),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        async with asyncio.timeout(self._ready_timeout):
            while not (output / "ready.json").exists():
                if process.returncode is not None:
                    raise RuntimeError(await _stderr(process))
                await asyncio.sleep(0.1)

    async def _start_remote(self, run: ExperimentRun) -> None:
        if self.hosts is None:
            raise ValueError("Remote UE sampling requires a monitoring manager")
        self.remote_dir = remote = f"/tmp/nador-ml-{run.run_id}"  # noqa: S108 - random run_id, created with mode 0700
        argv = [
            *("python3", f"{remote}/probe.py", "--out-dir", remote, "--stop-file", f"{remote}/STOP"),
            *("--run-id", run.run_id, "--namespace", self.hosts.namespace, "--kubeconfig", self.hosts.kubeconfig),
            *self.options.sampler_args(),
        ]
        sampler = {"sampler_dir": remote, "sampler_timeout_seconds": self._ready_timeout}
        inputs = {
            **sampler,
            "sampler_script": str(SAMPLER_SCRIPT),
            "sampler_argv": argv,
            "sampler_async_seconds": self.options.max_seconds + self.options.timeout_seconds + 30,
        }
        self.job_id = (await self.hosts.run(run, "start_sampler", inputs, outputs=["job_id"]))["job_id"]
        await self.hosts.run(run, "wait_sampler_ready", sampler)

    async def stop(self, run: ExperimentRun) -> None:
        """Stop sampling, bring its files back and check them."""
        if self.process is None and self.remote_dir is None:
            return
        status: dict[str, Any] = {"mode": "direct" if self.process else "remote", "remote_directory": self.remote_dir}
        try:
            if self.process is not None:
                await self._stop_local(run, self.process)
            elif self.hosts is not None:
                await self._stop_remote(run, self.hosts)
            self._check(run)
            status["status"] = "completed"
        except Exception as exc:
            status.update(status="error", error=str(exc))
            run.error("ue_context stop/fetch", exc)
        finally:
            run.save_json("ue_context/collection_status.json", status)

    async def _stop_local(self, run: ExperimentRun, process: asyncio.subprocess.Process) -> None:
        (run.directory / "ue_context" / "STOP").touch()
        try:
            async with asyncio.timeout(2 * self.options.timeout_seconds + 15):
                await process.wait()
        except TimeoutError:
            process.kill()
            await process.wait()
            raise RuntimeError("UE sampler did not stop in time") from None
        if process.returncode:
            raise RuntimeError(await _stderr(process))

    async def _stop_remote(self, run: ExperimentRun, hosts: ClusterHosts) -> None:
        output = run.directory / "ue_context"
        output.mkdir(parents=True, exist_ok=True)
        sampler = {"sampler_dir": self.remote_dir, "sampler_job_id": self.job_id}
        stop = {"sampler_timeout_seconds": self.options.timeout_seconds, "fetch_dir": str(output.resolve())}
        await hosts.run(run, "stop_sampler", {**sampler, **stop})
        await hosts.run(run, "cleanup_sampler", sampler)

    def _check(self, run: ExperimentRun) -> None:
        summary = json.loads((run.directory / "ue_context" / "summary.json").read_text())
        if summary.get("run_id") != run.run_id or not summary.get("samples"):
            raise ValueError("Missing or mismatched UE context samples")
        if summary.get("errors"):
            run.error(self.label, f"{summary['errors']} failed inventory polls")
        if summary.get("stop_reason") == "max_seconds":
            run.error(self.label, "max_seconds reached; history may not cover the full workload")


async def _stderr(process: asyncio.subprocess.Process) -> str:
    error = (await process.stderr.read(4000)).decode(errors="replace").strip() if process.stderr else ""
    return error or f"UE sampler exited with code {process.returncode}"
