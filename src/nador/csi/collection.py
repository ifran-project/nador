"""Collect csi_per_rb.csv from the node running a CSI-enabled gNB and measure that node's clock."""

import asyncio
import json
import shlex
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ifran.deployment.base import BaseManagement
from ifran.utils.ansible import get_ansible_inventory_line, run_ansible_role

from nador.artifacts.collectors import Collector
from nador.ssh import ssh_command

if TYPE_CHECKING:
    from nador.artifacts.experiment_run import ExperimentRun

ROLES = Path(__file__).resolve().parent.parent / "assets" / "roles"
CSI_FILE_NAME = "csi_per_rb.csv"
# The clock offset of the gNB node is applied to the windows only beyond this bound: below it, the SSH measurement
# error (half the round trip) is of the same order as the offset itself. The CSI batches are ~5 s long.
CLOCK_THRESHOLD_S = 0.5


class CsiCollector(Collector):
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
            raise ValueError(
                f"{component}: CSI logging is not enabled (set metrics.ran.csi and call nador.configure_ran_monitoring)"
            )
        if not deployed.node_name or deployed.node_name not in model.nodes:
            # The hostPath file lives on the pod's node: it must be known from the deployment model.
            raise ValueError(f"{component}: node_name must name a deployment node to locate the CSI file")
        self.component = component
        self.label = f"csi {component}"
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

    async def measure_clock(self, samples: int = 5) -> dict[str, Any]:
        """Return the gNB node clock offset (node - local, s) from the sample with the smallest round trip."""
        best = None
        for _ in range(samples):
            before = time.time()
            process = await asyncio.create_subprocess_exec(
                *ssh_command(self.node.ssh_info, self.node_name, "date", "+%s.%N"),
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

    async def start(self, run: "ExperimentRun") -> None:
        """Measure the gNB node clock before the workload, to put the run's windows on that clock."""
        clock = await self.measure_clock()
        offset = clock["offset_s"] if abs(clock["offset_s"]) > CLOCK_THRESHOLD_S else 0.0
        run.metadata.setdefault("csi", {})[self.component] = {
            "node": self.node_name,
            "clock_start": clock,
            "offset_applied_s": offset,
            "warnings": [],
        }

    async def collect(self, run: "ExperimentRun", start: float, end: float) -> None:
        """Cut the CSI file by the run's windows on the gNB node and fetch the cuts under csi/<component>/."""
        del start, end  # the windows come from timeline_summary.json, which covers the run's range
        record = run.metadata.setdefault("csi", {}).setdefault(
            self.component, {"node": self.node_name, "offset_applied_s": 0.0, "warnings": []}
        )
        try:
            clock_end = await self.measure_clock()
            record["clock_end"] = clock_end
            if "clock_start" in record:
                # Offset change over the run (clock drift or step), compared with the ~5 s CSI batches
                drift = clock_end["offset_s"] - record["clock_start"]["offset_s"]
                record["clock_drift_s"] = drift
                if abs(drift) > CLOCK_THRESHOLD_S:
                    record["warnings"].append(
                        f"{self.node_name} clock offset changed by {drift:+.3f} s during the run "
                        f"(threshold {CLOCK_THRESHOLD_S} s): CSI windows may be shifted"
                    )
        except Exception as exc:
            run.error(f"{self.label} clock", exc)
        destination = run.directory / "csi" / self.component
        summary = await self.fetch(destination, self._windows(run, destination, record["offset_applied_s"]))
        record["summary"] = summary
        if summary.get("dropped_rows"):
            record["warnings"].append(f"the logger dropped {summary['dropped_rows']} rows (buffer full)")

    def _windows(self, run: "ExperimentRun", destination: Path, offset: float) -> Path | None:
        """The run's timeline windows on the gNB node clock: csi/<component>/csi_windows.json."""
        timeline = run.directory / "timeline_summary.json"
        if not timeline.is_file():
            return None
        windows = [
            window | {"ran_start_epoch": window["start_epoch"] + offset, "ran_end_epoch": window["end_epoch"] + offset}
            for window in json.loads(timeline.read_text())["windows"]
        ]
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / "csi_windows.json"
        path.write_text(json.dumps({"node": self.node_name, "offset_applied_s": offset, "windows": windows}))
        return path

    async def fetch(self, dest_dir: str | Path, windows_json: str | Path | None = None, *, quiet: bool = True) -> dict:
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
        if result is None:
            raise RuntimeError(f"CSI collection on {self.node_name} returned no summary")
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


def csi_collectors(manager: BaseManagement) -> list[CsiCollector]:
    """A collector for each RAN component that logs CSI."""
    components = manager.deployment_model.ran.components
    return [
        CsiCollector(manager, name)
        for name, component in components.items()
        if str((component.extra_env_vars or {}).get("CSI_LOGGING_ENABLED")) == "1"
    ]
