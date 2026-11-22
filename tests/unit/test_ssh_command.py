"""The ssh command NADOR runs to reach Prometheus and Loki on the control node."""

from pathlib import Path

import pytest
from support import manager, profile

from nador.artifacts.ssh_tunnel import _ssh_forward_command


@pytest.fixture
def monitoring():
    managed = manager(profile())
    node = managed.deployment_model.nodes["node1"]
    node.ssh_info.hostname = "10.0.0.1"
    node.ssh_info.private_key_file = "~/.ssh/id_ed25519"
    return managed


def test_the_tunnel_uses_the_node_s_ssh_settings_and_never_a_shared_connection(monitoring):
    command = _ssh_forward_command(monitoring, 4000, 30095)
    assert command[:5] == ["ssh", "-N", "-T", "-S", "none"]
    assert "ControlMaster=no" in command
    assert command[command.index("-i") + 1] == str(Path("~/.ssh/id_ed25519").expanduser())
    assert command[-4:] == ["22", "-L", "127.0.0.1:4000:10.0.0.1:30095", "root@10.0.0.1"]


def test_a_deployment_without_a_control_node_cannot_be_tunnelled(monitoring):
    monitoring.deployment_model.params.k8s.master_node = None
    with pytest.raises(ValueError, match="no Kubernetes control node"):
        _ssh_forward_command(monitoring, 4000, 30095)
