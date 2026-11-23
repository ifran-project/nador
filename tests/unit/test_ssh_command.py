"""The ssh commands NADOR runs to reach the nodes: remote commands, and forwards to Prometheus, Loki, the CSI view."""

from pathlib import Path

import pytest
from support import manager, profile

from nador.artifacts.ssh_tunnel import prometheus_connection
from nador.ssh import ssh_command


@pytest.fixture
def ssh():
    info = manager(profile()).deployment_model.nodes["node1"].ssh_info
    info.hostname = "10.0.0.1"
    info.private_key_file = "~/.ssh/id_ed25519"
    return info


def test_a_forward_uses_the_node_s_ssh_settings_and_never_a_shared_connection(ssh):
    command = ssh_command(ssh, "node1", forward="127.0.0.1:4000:10.0.0.1:30095")
    assert command[:5] == ["ssh", "-T", "-N", "-S", "none"]
    assert command.index("ControlMaster=no") < command.index("BatchMode=yes")
    assert command[command.index("-i") + 1] == str(Path("~/.ssh/id_ed25519").expanduser())
    assert command[-4:] == ["22", "-L", "127.0.0.1:4000:10.0.0.1:30095", "root@10.0.0.1"]


def test_a_remote_command_keeps_the_connection_settings_of_ansible(ssh):
    ssh.common_args = "-o ProxyJump=gateway"
    command = ssh_command(ssh, "node1", "date", "+%s.%N")
    assert "-N" not in command
    assert "ControlMaster=no" not in command
    assert command[-3:] == ["root@10.0.0.1", "date", "+%s.%N"]
    assert command[command.index("ProxyJump=gateway") - 1] == "-o"


async def test_a_deployment_without_a_control_node_cannot_be_tunnelled():
    monitoring = manager(profile())
    monitoring.deployment_model.params.k8s.master_node = None
    with pytest.raises(ValueError, match="no Kubernetes control node"):
        async with prometheus_connection(monitoring):
            pass
