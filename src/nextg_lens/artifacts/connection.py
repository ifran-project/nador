"""Temporary Prometheus access for artifact collection and metric queries."""

import asyncio
import shlex
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from nextg_lens.deployment import MonitoringManagement


@asynccontextmanager
async def _connection(monitoring: MonitoringManagement, node_port: int, service: str) -> AsyncIterator[str]:
    """Open a temporary SSH forward using the monitoring manager's SSH settings."""
    model = monitoring.deployment_model
    name = model.params.k8s.master_node
    node = model.nodes[name]
    ssh = node.ssh_info
    host = ssh.hostname or name
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    command = ["ssh", "-N", "-T", "-S", "none"]
    for option in (
        "ControlMaster=no",
        "ControlPersist=no",
        "BatchMode=yes",
        "ExitOnForwardFailure=yes",
        "ConnectTimeout=10",
        "ServerAliveInterval=30",
        "ServerAliveCountMax=3",
    ):
        command += ["-o", option]
    command += shlex.split(ssh.common_args or "")
    if ssh.private_key_file:
        command += ["-i", str(Path(ssh.private_key_file).expanduser())]
    command += [
        "-p",
        str(ssh.port),
        "-L",
        f"127.0.0.1:{port}:{host}:{node_port}",
        f"{ssh.user}@{host}",
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    url = f"http://127.0.0.1:{port}"

    try:
        async with asyncio.timeout(20):
            while True:
                if process.returncode is not None:
                    _, error = await process.communicate()
                    raise RuntimeError(service + " SSH tunnel failed: " + error.decode(errors="replace").strip())
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(0.2)
        yield url
    finally:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                process.kill()
                await process.wait()


@asynccontextmanager
async def prometheus_connection(monitoring: MonitoringManagement) -> AsyncIterator[str]:
    """Open Prometheus access only for the duration of collection."""
    async with _connection(monitoring, monitoring.deployment_model.monitoring.prometheus_node_port, 'Prometheus') as url:
        yield url


@asynccontextmanager
async def loki_connection(monitoring: MonitoringManagement) -> AsyncIterator[str]:
    """Open Loki access through the same route as the monitoring manager."""
    async with _connection(monitoring, monitoring.deployment_model.monitoring.loki_node_port, 'Loki') as url:
        yield url
