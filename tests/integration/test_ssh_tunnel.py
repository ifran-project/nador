"""SSH tunnels to Prometheus and Loki, with a fake ``ssh`` that opens the forwarded port locally."""

import os
import socket
import sys

import pytest
from support import manager, profile

from nador.artifacts.ssh_tunnel import loki_connection, prometheus_connection

# Listens on the local end of `-L 127.0.0.1:PORT:host:node_port` until terminated, like ssh -N.
FAKE_SSH = f"""#!{sys.executable}
import os, socket, sys, time
if os.environ.get("FAKE_SSH_FAIL"):
    sys.stderr.write("Permission denied (publickey).\\n")
    sys.exit(255)
port = int(sys.argv[sys.argv.index("-L") + 1].split(":")[1])
with open(os.environ["FAKE_SSH_LOG"], "w") as log:
    log.write(" ".join(sys.argv[1:]))
listener = socket.create_server(("127.0.0.1", port))
while True:
    connection, _ = listener.accept()
    connection.close()
"""


@pytest.fixture
def ssh(tmp_path, monkeypatch):
    """Put the fake ssh first on PATH; returns the file it writes its arguments to."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "ssh").write_text(FAKE_SSH)
    (bin_dir / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    log = tmp_path / "ssh-args"
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    return log


@pytest.fixture
def monitoring():
    managed = manager(profile())
    node = managed.deployment_model.nodes["node1"]
    node.ssh_info.hostname = "10.0.0.1"
    node.ssh_info.private_key_file = "~/.ssh/id_ed25519"
    return managed


@pytest.mark.integration
async def test_prometheus_is_reachable_through_the_tunnel_while_it_is_open(ssh, monitoring):
    async with prometheus_connection(monitoring) as url:
        port = int(url.rsplit(":", 1)[1])
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    assert ssh.read_text().endswith(f"-L 127.0.0.1:{port}:10.0.0.1:30095 root@10.0.0.1")
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port), timeout=2).close()


@pytest.mark.integration
async def test_loki_uses_its_own_node_port(ssh, monitoring):
    async with loki_connection(monitoring):
        pass
    assert ":10.0.0.1:31000 " in ssh.read_text()


@pytest.mark.integration
async def test_a_refused_tunnel_reports_ssh_s_error(ssh, monitoring, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_FAIL", "1")
    with pytest.raises(RuntimeError, match=r"Prometheus SSH tunnel failed: Permission denied \(publickey\)"):
        async with prometheus_connection(monitoring):
            pass
