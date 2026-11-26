"""The ssh commands NADOR runs to reach the nodes: remote commands, and forwards to Prometheus, Loki, the CSI view."""

from pathlib import Path

import pytest
from ifran.models.provisioning import SSHInfo
from support import manager, profile

from nador.artifacts.ssh_tunnel import prometheus_connection
from nador.ssh import inventory_line, node_ssh, ssh_command


@pytest.fixture
def ssh():
    return SSHInfo(hostname="10.0.0.1", user="root", private_key_file="~/.ssh/id_ed25519")


def test_a_forward_uses_the_node_s_ssh_settings_and_never_a_shared_connection(ssh):
    command = ssh_command(ssh, "node1", forward="127.0.0.1:4000:10.0.0.1:30095")
    assert command[:5] == ["ssh", "-T", "-N", "-S", "none"]
    assert command.index("ControlMaster=no") < command.index("BatchMode=yes")
    assert command[command.index("-i") + 1] == str(Path("~/.ssh/id_ed25519").expanduser())
    assert command[-4:] == ["22", "-L", "127.0.0.1:4000:10.0.0.1:30095", "root@10.0.0.1"]


def test_a_remote_command_keeps_the_connection_settings_of_ansible(ssh):
    ssh.extra_args = "-o ProxyJump=gateway"
    command = ssh_command(ssh, "node1", "date", "+%s.%N")
    assert "-N" not in command
    assert "ControlMaster=no" not in command
    assert command[-3:] == ["root@10.0.0.1", "date", "+%s.%N"]
    assert command[command.index("ProxyJump=gateway") - 1] == "-o"


def test_the_connection_timeout_is_the_one_of_the_ssh_settings(ssh):
    ssh.timeout = 7
    assert "ConnectTimeout=7" in ssh_command(ssh, "node1", "true")


def test_a_node_gets_the_deployment_ssh_defaults_under_its_own_settings():
    model = manager(profile()).deployment_model
    model.params.ssh = SSHInfo(user="admin", port=2222, private_key_file="~/.ssh/lab", jumphost=SSHInfo(hostname="gw"))
    model.nodes["node1"].ssh_info = SSHInfo(hostname="10.0.0.1", port=22)

    ssh = node_ssh(model, "node1")
    assert (ssh.hostname, ssh.port, ssh.user, ssh.private_key_file) == (
        "10.0.0.1",
        22,
        "admin",
        str(Path("~/.ssh/lab").expanduser()),
    )
    command = ssh_command(ssh, "node1", "true")
    assert "admin@10.0.0.1" in command
    assert any("gw" in part for part in command)  # the jump host of the defaults
    assert inventory_line(model, "node1").split()[:2] == ["node1", "ansible_host=10.0.0.1"]
    assert "ansible_user=admin" in inventory_line(model, "node1").split()


async def test_a_deployment_without_a_control_node_cannot_be_tunnelled():
    monitoring = manager(profile())
    monitoring.deployment_model.params.k8s.master_node = None
    with pytest.raises(ValueError, match="no Kubernetes control node"):
        async with prometheus_connection(monitoring):
            pass
