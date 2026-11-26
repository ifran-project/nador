"""Temporary Prometheus access for artifact collection and metric queries."""

import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from nador.deployment import MonitoringManagement
from nador.ssh import close_forward, node_ssh, open_forward, ssh_command


@asynccontextmanager
async def _connection(monitoring: MonitoringManagement, node_port: int, service: str) -> AsyncIterator[str]:
    """Forward a free local port to node_port of the Kubernetes control node while the context is open."""
    model = monitoring.deployment_model
    name = model.params.k8s.master_node
    if not name or name not in model.nodes:
        raise ValueError("The deployment model has no Kubernetes control node to tunnel through")
    ssh = node_ssh(model, name)
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    forward = f"127.0.0.1:{port}:{ssh.hostname or name}:{node_port}"
    process = await open_forward(ssh_command(ssh, name, forward=forward), port, name=service)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await close_forward(process)


@asynccontextmanager
async def prometheus_connection(monitoring: MonitoringManagement) -> AsyncIterator[str]:
    """Open Prometheus access only for the duration of collection."""
    async with _connection(
        monitoring, monitoring.deployment_model.monitoring.prometheus_node_port, "Prometheus"
    ) as url:
        yield url


@asynccontextmanager
async def loki_connection(monitoring: MonitoringManagement) -> AsyncIterator[str]:
    """Open Loki access through the same route as the monitoring manager."""
    async with _connection(monitoring, monitoring.deployment_model.monitoring.loki_node_port, "Loki") as url:
        yield url
