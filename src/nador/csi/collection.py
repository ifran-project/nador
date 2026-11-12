"""Collect csi_per_rb.csv from the node running a CSI-enabled gNB and measure that node's clock."""

import asyncio
import json
import shlex
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestrator.deployment.base import BaseManagement
from orchestrator.utils.ansible import get_ansible_inventory_line, run_ansible_role

ROLES = Path(__file__).resolve().parent.parent / "assets" / "roles"
CSI_FILE_NAME = "csi_per_rb.csv"


class CsiCollector:
    """
    CSI log of one gNB component, read on the node it is pinned to (component.node_name).

    The logger timestamps flush batches (~5 s, 1 s resolution) with the gNB node clock, which is
    not synchronized with the notebook host. measure_clock() estimates the offset of that clock
    from the notebook clock over ssh (minimum round trip of several samples), so that experiment
    windows can be expressed on the gNB node clock.
    """

    def __init__(
        self,
        manager: BaseManagement,
        component: str = "gnb",
        *,
        fetch_full: bool = True,
        window_types: str = "section",
        padding_seconds: float = 0.0,
        cpu_list: str = "",
        cleanup_remote: bool = True,
    ) -> None:
        model = manager.deployment_model
        if component not in model.ran.components:
            raise ValueError(f"Unknown RAN component {component!r}")
        deployed = model.ran.components[component]
        env = deployed.extra_env_vars or {}
        if str(env.get("CSI_LOGGING_ENABLED")) != "1":
            raise ValueError(f"{component}: CSI logging is not enabled (see nador.csi.enable_csi)")
        if not deployed.node_name or deployed.node_name not in model.nodes:
            # The hostPath file lives on the pod's node: it must be known from the deployment model.
            raise ValueError(f"{component}: node_name must name a deployment node to locate the CSI file")
        self.component = component
        self.node_name = deployed.node_name
        self.node = model.nodes[deployed.node_name].model_copy(deep=True)
        self.ansible = model.params.ansible.model_copy(deep=True)
        self.src_file = f"{env.get('CSI_OUTPUT_DIR', '/data/csi').rstrip('/')}/{CSI_FILE_NAME}"
        self.provenance = {
            "component": component,
            "node": self.node_name,
            "namespace": deployed.namespace,
            "image": deployed.image.model_dump(mode="json") if deployed.image else None,
            "csi_env": {k: v for k, v in env.items() if k.startswith("CSI_")},
        }
        self.fetch_full = fetch_full
        self.window_types = window_types
        self.padding_seconds = padding_seconds
        low_prio = "nice -n 19 ionice -c 3"
        # Never give CPUs of the isolated range used by real-time gNB threads.
        self.low_prio = f"{low_prio} taskset -c {shlex.quote(cpu_list)}" if cpu_list else low_prio
        self.cleanup_remote = cleanup_remote

    def _ssh_command(self, remote: list[str]) -> list[str]:
        ssh = self.node.ssh_info
        command = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
        # common_args carries the jump host and the connection multiplexing settings used by Ansible.
        command += shlex.split(ssh.common_args or "")
        if ssh.private_key_file:
            command += ["-i", str(Path(ssh.private_key_file).expanduser())]
        command += ["-p", str(ssh.port), f"{ssh.user}@{ssh.hostname or self.node_name}", *remote]
        return command

    async def measure_clock(self, samples: int = 5) -> dict[str, Any]:
        """Return the gNB node clock offset (node - local, s) from the sample with the smallest round trip."""
        best = None
        for _ in range(samples):
            before = time.time()
            process = await asyncio.create_subprocess_exec(
                *self._ssh_command(["date", "+%s.%N"]),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await process.communicate()
            after = time.time()
            if process.returncode != 0:
                raise RuntimeError(f"Clock read on {self.node_name} failed: {err.decode(errors='replace').strip()}")
            remote = float(out.decode().strip())
            rtt = after - before
            if best is None or rtt < best["rtt_s"]:
                best = {"offset_s": remote - (before + after) / 2, "rtt_s": rtt, "local_epoch": (before + after) / 2}
        assert best is not None
        # The remote read happened somewhere inside the round trip: half of it bounds the error.
        return best | {"uncertainty_s": best["rtt_s"] / 2, "samples": samples, "node": self.node_name}

    async def collect(
        self, dest_dir: str | Path, windows_json: str | Path | None = None, *, quiet: bool = True
    ) -> dict:
        """Read the CSI file on the gNB node and fetch csi_per_rb.csv.gz and per-window files to dest_dir."""
        dest = Path(dest_dir).resolve()  # noqa: ASYNC240 (local metadata, no I/O wait)
        dest.mkdir(parents=True, exist_ok=True)
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        result = await run_ansible_role(
            role="csi/collect",
            runner_path=str(Path(self.ansible.runner_path) / "csi" / self.component),
            inputs={
                "csi_src_file": self.src_file,
                "csi_dest_dir": str(dest),
                "csi_run_id": run_id,
                "csi_fetch_full": self.fetch_full,
                "csi_windows_json": str(Path(windows_json).resolve()) if windows_json else "",  # noqa: ASYNC240
                "csi_window_types": self.window_types,
                "csi_padding": self.padding_seconds,
                "csi_low_prio": self.low_prio,
                "csi_cleanup_remote": self.cleanup_remote,
            },
            output_keys=["csi_summary"],
            roles_paths=[str(ROLES), *self.ansible.roles_paths],
            hosts=self.node_name,
            inventory=get_ansible_inventory_line(self.node_name, self.node),
            become=True,
            quiet=quiet,
        )
        summary = result["csi_summary"]
        archive = dest / "by_window.tar"
        if archive.is_file():  # noqa: ASYNC240 (local file, no I/O wait)
            # Python tarfile instead of the controller tar (BSD tar on macOS); "data" rejects unsafe members.
            with tarfile.open(archive) as tar:
                tar.extractall(dest, filter="data")
            archive.unlink()
        (dest / "csi_collection.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "source_file": self.src_file,
                    "provenance": self.provenance,
                    "summary": summary,
                    "windows_json": str(windows_json or ""),
                    "window_types": self.window_types,
                    "padding_seconds": self.padding_seconds,
                    "note": "CSI timestamps and windows are on the gNB node clock.",
                },
                indent=2,
            )
        )
        return summary
