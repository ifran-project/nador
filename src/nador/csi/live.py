"""Live CSI view (streamer + Streamlit visualizer v9.0) on a node other than the gNB node."""

import atexit
import os
import shlex
import signal
import socket
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from ifran.deployment.base import BaseManagement
from ifran.utils.ansible import get_ansible_inventory_line, run_ansible_role

from nador.csi.collection import ROLES, CsiCollector
from nador.ssh import close_forward, open_forward, ssh_command

if TYPE_CHECKING:
    import asyncio

LIVE_ROOT = "/root/csi_live"  # on the live node


def _local_port_free(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _terminate(pid: int) -> None:
    """Stop the tunnel when the notebook kernel exits, if it still runs."""
    with suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)


def _free_local_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class CsiLiveView:
    """
    Follow the CSI log of a gNB from live_node (e.g. the monitoring node) and show it in Streamlit.

    Only an sshd session and "tail -F" at the lowest priority run on the gNB node. Views of several
    components (gnb, du1, ...) can share live_node: each has its own directory, key and forced command,
    and needs its own port. The view listens
    on 127.0.0.1:port of live_node; open_tunnel() forwards it to the notebook host, like the Grafana
    tunnel of the example notebooks (tunnel_command() gives the same ssh command for a terminal).
    The live copy is for display only: collection still reads the gNB node file.
    """

    def __init__(
        self,
        manager: BaseManagement,
        live_node: str,
        component: str = "gnb",
        *,
        ran_address: str | None = None,
        port: int = 8501,
        window_s: int = 120,
        refresh_s: int = 10,
        max_mb: int = 1024,
        visualizer_src: str = "",
        visualizer_ref: str = "csi-v3.1",
        cpu_list: str = "",
    ) -> None:
        source = CsiCollector(manager, component)  # same checks: CSI enabled, gNB node known
        model = manager.deployment_model
        if live_node not in model.nodes:
            raise ValueError(f"Unknown deployment node {live_node!r}")
        if live_node == source.node_name:
            raise ValueError("The live view must not run on the gNB node")
        self.source = source
        self.live_node = live_node
        self.live = model.nodes[live_node].model_copy(deep=True)
        self.ansible = model.params.ansible.model_copy(deep=True)
        ran_ssh = source.node.ssh_info
        output_dir, file_name = source.src_file.rsplit("/", 1)
        self.port = port
        self.tunnel: asyncio.subprocess.Process | None = None
        # Tunnel stderr goes to a file, not a pipe: nobody reads a pipe of a tunnel open for hours.
        self.tunnel_log = Path(self.ansible.runner_path).expanduser() / "csi" / "live" / component / "tunnel.log"
        self.inputs = {
            "csi_ran_host": source.node_name,
            # Address of the gNB node as reached from the live node (same as from the jump host by default)
            "csi_live_ran_address": ran_address or ran_ssh.hostname or source.node_name,
            "csi_live_ran_user": ran_ssh.user,
            "csi_output_dir": output_dir,
            "csi_file_name": file_name,
            "csi_cpu_list": cpu_list,
            "csi_live_port": port,
            "csi_live_window_s": window_s,
            "csi_live_refresh_s": refresh_s,
            "csi_live_max_mb": max_mb,
            "csi_live_visualizer_src": str(Path(visualizer_src).expanduser().resolve()) if visualizer_src else "",
            "csi_live_visualizer_ref": visualizer_ref,
            # One directory, key and forced command per component; one venv for all of them
            "csi_live_dir": f"{LIVE_ROOT}/{component}",
            "csi_live_venv": f"{LIVE_ROOT}/venv",
            "csi_live_source_script": f"/usr/local/bin/csi_live_source_{component}.sh",
            "csi_live_key_comment": f"csi-live-{component}@{live_node}",
        }

    async def _run(self, action: str, quiet: bool) -> dict:
        inventory = "\n".join(
            get_ansible_inventory_line(name, node)
            for name, node in ((self.live_node, self.live), (self.source.node_name, self.source.node))
        )
        result = await run_ansible_role(
            role="csi/live",
            runner_path=str(Path(self.ansible.runner_path) / "csi" / "live" / self.source.component),
            inputs=self.inputs | {"csi_live_action": action},
            output_keys=["csi_live_status"] if action == "status" else None,
            roles_paths=[str(ROLES), *self.ansible.roles_paths],
            hosts=self.live_node,
            inventory=inventory,
            become=True,
            quiet=quiet,
        )
        return result or {}

    async def start(self, *, quiet: bool = True) -> str:
        """(Re)start the streamer and the viewer, then open the tunnel; return the local URL."""
        await self._run("start", quiet)
        return await self.open_tunnel()

    async def status(self, *, quiet: bool = True) -> str:
        """Processes, size and age of the live copy, last log lines."""
        result = await self._run("status", quiet)
        return "\n".join(result["csi_live_status"])

    async def stop(self, *, quiet: bool = True) -> None:
        """Close the tunnel, stop both processes, revoke the live key and remove the live copy."""
        await self.close_tunnel()
        await self._run("stop", quiet)

    def _tunnel_argv(self, local_port: int) -> list[str]:
        forward = f"127.0.0.1:{local_port}:127.0.0.1:{self.port}"
        return ssh_command(self.live.ssh_info, self.live_node, forward=forward)

    def tunnel_command(self, local_port: int | None = None) -> str:
        """The ssh command of open_tunnel(), to run in a terminal instead."""
        return shlex.join(self._tunnel_argv(local_port or self.port))

    async def open_tunnel(self, local_port: int | None = None, timeout_s: float = 20) -> str:
        """Forward 127.0.0.1:local_port of the notebook host to the viewer; return its URL.

        Without local_port, the viewer port is used if it is free on the notebook host, else a free port chosen
        by the system (e.g. a local Streamlit already listens there).
        """
        await self.close_tunnel()
        # A busy port would make the readiness check below connect to whatever already listens there.
        if not _local_port_free(local_port or self.port):
            if local_port:
                raise RuntimeError(f"Local port {local_port} is already in use; pass another local_port=")
            local_port = _free_local_port()
        local_port = local_port or self.port
        self.tunnel = await open_forward(
            self._tunnel_argv(local_port), local_port, name="CSI live", log=self.tunnel_log, timeout_s=timeout_s
        )
        atexit.register(_terminate, self.tunnel.pid)  # close it when the kernel exits normally
        return f"http://127.0.0.1:{local_port}"

    async def close_tunnel(self) -> None:
        """Close the tunnel opened by open_tunnel(), if any."""
        if self.tunnel is not None:
            await close_forward(self.tunnel)
        self.tunnel = None
