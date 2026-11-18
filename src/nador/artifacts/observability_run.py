"""Native experiment context collection, with no cluster operations on import."""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import urlsplit

from ifran.utils.ansible import get_ansible_inventory_line, run_ansible_role
from pydantic import BaseModel, ConfigDict, Field, field_validator

from nador.planner import ASSETS

from . import ue_sampler
from .experiment_run import ExperimentRun
from .loki_export import collect_logs
from .ssh_tunnel import loki_connection

if TYPE_CHECKING:
    from nador.deployment import MonitoringManagement


class ObservabilityOptions(BaseModel):
    """What ObservabilityRun collects and how hard it may poll."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    mapper_url: str | None = None
    collect_setup: bool = True
    collect_ue_context: bool = True
    setup_hosts: list[Annotated[str, Field(min_length=1)]] | None = None
    interval_seconds: float = Field(default=2, ge=0.1, le=3600, allow_inf_nan=False)
    timeout_seconds: int = Field(default=5, ge=1, le=60)
    max_seconds: int = Field(default=7200, ge=1)
    limit: int = Field(default=5000, ge=1)
    namespace: str | None = None
    service: str = "ue-mapper-api"
    service_port: int = Field(default=8080, ge=1, le=65535)
    loki_url: str | None = None
    collect_loki: bool = False
    loki_selector: str = '{namespace=~".+"}'
    loki_limit: int = Field(default=5000, ge=1, le=100_000)
    loki_max_queries: int = Field(default=1024, ge=1, le=100_000)

    @field_validator("mapper_url")
    @classmethod
    def _check_mapper_url(cls, url: str | None) -> str | None:
        if url:
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment:
                raise ValueError("mapper_url must be an HTTP(S) base URL without query or fragment")
        return url

    def sampler_args(self) -> dict:
        """Arguments shared by the local and remote UE context samplers."""
        return self.model_dump(
            include={"interval_seconds", "timeout_seconds", "max_seconds", "limit", "service", "service_port"}
        )


class ObservabilityRun(ExperimentRun):
    """Collect setup before/after a workload and bounded sampled UE inventory.

    A monitoring manager enables remote host/cluster snapshots and service-proxy
    sampling. A mapper_url instead samples directly from this process. Sampling
    observes inventory, not every UE transition. Collection errors follow
    ExperimentRun's policy: fail successful workloads, but never replace a
    workload exception.

    Keyword arguments are either ObservabilityOptions fields or ExperimentRun
    arguments (name, prometheus_url, queries_file, step, timezone).
    """

    def __init__(self, directory: str | Path, *, monitoring: MonitoringManagement | None = None, **kwargs: Any) -> None:
        settings = {key: kwargs.pop(key) for key in list(kwargs) if key in ObservabilityOptions.model_fields}
        options = ObservabilityOptions(**settings)
        kwargs.setdefault("queries_file", ASSETS / "observability_queries.json")
        super().__init__(directory, monitoring=monitoring, **kwargs)
        if options.setup_hosts and (
            monitoring is None or any(host not in monitoring.deployment_model.nodes for host in options.setup_hosts)
        ):
            raise ValueError("setup_hosts must refer to nodes in the supplied monitoring manager")
        self.settings = options
        self.mapper_url = options.mapper_url
        self.loki_url = options.loki_url
        self.collect_loki = options.collect_loki or bool(options.loki_url)
        self.options = options.sampler_args()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = None
        self._thread_error = None
        self._remote_directory = None
        self._job_id = None
        self._remote_config = None

    async def _finalize(self, ended):
        async with AsyncExitStack() as access:
            if self.collect_loki and not self.loki_url and self.monitoring is not None:
                try:
                    self.loki_url = await access.enter_async_context(loki_connection(self.monitoring))
                except Exception as exc:
                    self._error("loki access", exc)
            await super()._finalize(ended)

    def _collect(self, ended):
        super()._collect(ended)
        if self.loki_url:
            try:
                summary = collect_logs(
                    self.directory / "logs",
                    url=self.loki_url,
                    selector=self.settings.loki_selector,
                    start=self.started,
                    end=ended,
                    limit=self.settings.loki_limit,
                    max_queries=self.settings.loki_max_queries,
                )
                if summary.get("error"):
                    self._error("loki", summary["error"])
                if summary.get("possible_truncation"):
                    detail = (
                        f"query budget exhausted ({summary['queries']}/{summary['max_queries']}); "
                        if summary.get("query_budget_exhausted")
                        else ""
                    )
                    self._error("loki", detail + "possible truncation; log export may be incomplete")
            except Exception as exc:
                self._error("loki", exc)

    def _error(self, label, exc):
        self.metadata["collection_errors"].append(f"{label}: {exc}")

    def _write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    @property
    def _model(self):
        if self.monitoring is None:
            raise ValueError("Remote collection requires a monitoring manager")
        return self.monitoring.deployment_model

    def _control(self):
        if self._remote_config is None:
            model = self._model
            cfg = model.monitoring
            name = model.params.k8s.master_node
            if not name or name not in model.nodes:
                raise ValueError("ML context requires a control node in monitoring.deployment_model.nodes")
            self._remote_config = (name, cfg.kubeconfig, self.settings.namespace or cfg.namespace)
        return self._remote_config

    async def _run(self, action: str, inputs: dict, *, host: str | None = None, output_keys=None):
        """Run one action of the experiment/context Ansible role on a cluster host."""
        model = self._model
        host = host or self._control()[0]
        result = await run_ansible_role(
            role="experiment/context",
            hosts=host,
            inventory=get_ansible_inventory_line(host, model.nodes[host]),
            become=True,
            inputs={"context_action": action, **inputs},
            output_keys=output_keys,
            runner_path=str(Path(model.params.ansible.runner_path) / "ml-artifacts" / self.run_id / action),
            roles_paths=[str(ASSETS / "roles"), *model.params.ansible.roles_paths],
            quiet=True,
        )
        return result or {}

    async def _snapshot(self, phase):
        if not self.monitoring or not self.settings.collect_setup:
            return
        output = self.directory / "setup" / phase
        script = Path(__file__).with_name("setup_snapshot.py")
        hosts = []
        nodes = self.monitoring.deployment_model.nodes
        selected = self.settings.setup_hosts
        if selected is None:
            control = self._control()[0]
            selected = [
                name
                for name, node in nodes.items()
                if name == control or getattr(getattr(node, "ssh_info", None), "hostname", None)
            ]
        for host in selected:
            try:
                snapshot = await self._read_snapshot(script, ["--mode", "host"], host)
                hosts.append({"host": host, **snapshot})
            except Exception as exc:
                self._error(f"setup {phase} host {host}", exc)
                hosts.append({"host": host, "status": "error", "error": str(exc)})
        self._write(output / "hosts.json", hosts)
        try:
            host, kubeconfig, _ = self._control()
            snapshot = await self._read_snapshot(script, ["--mode", "cluster", "--kubeconfig", kubeconfig], host)
            self._write(output / "cluster.json", snapshot)
        except Exception as exc:
            self._error(f"setup {phase} cluster", exc)
            self._write(output / "cluster.json", {"status": "error", "error": str(exc)})

    async def _read_snapshot(self, script, args, host):
        result = await self._run(
            "snapshot", {"snapshot_script": str(script), "snapshot_args": args}, host=host, output_keys=["snapshot"]
        )
        snapshot = json.loads(result["snapshot"])
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("data"), dict):
            raise ValueError("Invalid setup snapshot")
        for name, value in snapshot["data"].items():
            if isinstance(value, dict) and value.get("status") == "error":
                self._error(f"setup {host} {name}", value.get("error", "command failed"))
        return snapshot

    def _sample_direct(self, args):
        try:
            ue_sampler.sample(args, lambda: ue_sampler.fetch_inventory(args, []), self._stop, self._ready)
        except Exception as exc:
            self._thread_error = exc
        finally:
            self._ready.set()

    async def _start_sampler(self):
        if not self.settings.collect_ue_context or not (self.mapper_url or self.monitoring):
            return
        if self.mapper_url:
            args = SimpleNamespace(
                **self.options,
                url=self.mapper_url,
                out_dir=str(self.directory / "ue_context"),
                stop_file=str(self.directory / "ue_context" / "STOP"),
                run_id=self.run_id,
            )
            self._thread = threading.Thread(target=self._sample_direct, args=(args,), name=f"ue-context-{self.run_id}")
            self._thread.start()
            await asyncio.to_thread(self._ready.wait)
            if self._thread_error:
                raise self._thread_error
            return
        _, kubeconfig, namespace = self._control()
        self._remote_directory = "/tmp/nador-ml-" + self.run_id  # noqa: S108 - random run_id, created with mode 0700
        remote = self._remote_directory
        argv = [
            "python3",
            remote + "/probe.py",
            "--out-dir",
            remote,
            "--stop-file",
            remote + "/STOP",
            "--run-id",
            self.run_id,
            "--namespace",
            namespace,
            "--kubeconfig",
            kubeconfig,
        ]
        for key, value in self.options.items():
            argv += ["--" + key.replace("_", "-"), str(value)]
        sampler = {"sampler_dir": remote, "sampler_timeout_seconds": self.options["timeout_seconds"] + 15}
        result = await self._run(
            "start_sampler",
            {
                **sampler,
                "sampler_script": str(Path(__file__).with_name("ue_sampler.py")),
                "sampler_argv": argv,
                "sampler_async_seconds": self.options["max_seconds"] + self.options["timeout_seconds"] + 30,
            },
            output_keys=["job_id"],
        )
        self._job_id = result["job_id"]
        await self._run("wait_sampler_ready", sampler)

    async def _fetch_remote_samples(self, output: Path) -> None:
        """Stop the remote sampler, fetch whatever it recorded, then remove its directory."""
        sampler = {"sampler_dir": self._remote_directory, "sampler_job_id": self._job_id or ""}
        output.mkdir(parents=True, exist_ok=True)
        await self._run(
            "stop_sampler",
            {**sampler, "sampler_timeout_seconds": self.options["timeout_seconds"], "fetch_dir": str(output.resolve())},
        )
        await self._run("cleanup_sampler", sampler)

    def _check_samples(self, output: Path) -> None:
        summary = json.loads((output / "summary.json").read_text())
        if summary.get("run_id") != self.run_id or not summary.get("samples"):
            raise ValueError("Missing or mismatched UE context samples")
        if summary.get("errors"):
            self._error("ue_context", f"{summary['errors']} failed inventory polls")
        if summary.get("stop_reason") == "max_seconds":
            self._error("ue_context", "max_seconds reached; history may not cover the full workload")

    async def _stop_local_sampler(self) -> None:
        self._stop.set()
        if self._thread is not None:
            await asyncio.to_thread(self._thread.join)
        if self._thread_error:
            raise self._thread_error

    async def _stop_sampler(self):
        if not (self._thread or self._remote_directory):
            return
        output = self.directory / "ue_context"
        status = {"mode": "direct" if self.mapper_url else "remote", "remote_directory": self._remote_directory}
        try:
            if self._thread:
                await self._stop_local_sampler()
            else:
                await self._fetch_remote_samples(output)
            self._check_samples(output)
            status["status"] = "completed"
        except Exception as exc:
            status.update(status="error", error=str(exc))
            self._error("ue_context stop/fetch", exc)
        finally:
            self._write(output / "collection_status.json", status)

    async def __aenter__(self):
        """Start the run, take the 'before' snapshot and start UE context sampling."""
        await super().__aenter__()

        async def start():
            for label, operation in [
                ("setup before", lambda: self._snapshot("before")),
                ("ue_context start", self._start_sampler),
            ]:
                try:
                    await operation()
                except Exception as exc:
                    self._error(label, exc)

        task = asyncio.create_task(start())
        try:
            await asyncio.shield(task)
        except BaseException as exc:
            # Finish startup before stopping: otherwise an async remote launch can
            # race cleanup and leave an untracked sampler running.
            await self._drain(task)
            await self.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        return self

    @staticmethod
    async def _drain(task):
        cancelled = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancelled = exc
            except Exception:
                break
        return cancelled

    async def __aexit__(self, exc_type, exc, tb):
        """Stop sampling, take the 'after' snapshot, then finalize like ExperimentRun."""

        async def finish():
            for label, operation in [
                ("ue_context cleanup", self._stop_sampler),
                ("setup after", lambda: self._snapshot("after")),
            ]:
                try:
                    await operation()
                except Exception as error:
                    self._error(label, error)
            return await super(ObservabilityRun, self).__aexit__(exc_type, exc, tb)

        task = asyncio.create_task(finish())
        cancelled = await self._drain(task)
        if cancelled:
            # A collection failure must not mask cancellation either.
            if not task.cancelled():
                task.exception()
            self.metadata["status"] = "cancelled"
            self._save_metadata()
            raise cancelled
        return task.result()
