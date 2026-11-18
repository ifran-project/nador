"""The iperf shortcut, with IFRAN's managers and Ansible replaced by recorders."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from nador import traffic


def address(ip):
    return json.dumps([{"addr_info": [{"family": "inet", "local": ip}, {"family": "inet6", "local": "fe80::1"}]}])


class Containers:
    """Stands in for an IFRAN UEManagement: commands run in UE containers."""

    def __init__(self, outputs):
        self.calls = []
        self.outputs = outputs
        node = SimpleNamespace(name="node4", ssh_info=SimpleNamespace(hostname="10.0.0.4"))
        self.deployment_model = SimpleNamespace(
            nodes={"node4": node}, params=SimpleNamespace(ansible=SimpleNamespace(runner_path="runner"))
        )

    async def run_command(self, command, **kwargs):
        self.calls.append((command, kwargs))
        targets = kwargs.get("components") or kwargs.get("resources")
        if command[0] == "ip":
            return {name: address(self.outputs[name]) for name in targets}
        if command[0] == "iperf3":
            return {targets[0]: json.dumps({"end": {}, "command": command})}
        return dict.fromkeys(targets, "")


class Machines(Containers):
    """Stands in for an IFRAN ProvisioningManagement: commands run over SSH on UE machines."""


@pytest.fixture
def ansible(monkeypatch):
    calls = []

    async def playbook(**kwargs):
        calls.append(("playbook", kwargs))

    async def command(**kwargs):
        calls.append(("command", kwargs))
        return ""

    monkeypatch.setattr(traffic, "run_ansible_playbook", playbook)
    monkeypatch.setattr(traffic, "run_ansible_command", command)
    monkeypatch.setattr(traffic, "get_ansible_inventory_line", lambda name, node: f"{name} inventory")
    monkeypatch.setattr(traffic, "ProvisioningManagement", Machines)
    return calls


def test_setup_installs_the_server_and_finds_ue_addresses(ansible):
    ue = Containers({"sim_ue": "10.45.0.2", "sim_ue2": "10.45.0.3"})
    iperf = asyncio.run(traffic.Iperf.setup(ue, server="node4", interface="oaitun_ue1", clients=["sim_ue", "sim_ue2"]))
    assert iperf.server_ip == "10.0.0.4"
    assert iperf.ue_ips == {"sim_ue": "10.45.0.2", "sim_ue2": "10.45.0.3"}
    assert ansible[0][0] == "playbook"
    assert "apt-get install -y iperf3" in ue.calls[0][0][-1]
    assert all("components" in kwargs for _, kwargs in ue.calls)


def test_ue_machines_get_iperf_installed_with_sudo(ansible):
    machines = Machines({"pacify": "10.45.0.9"})
    asyncio.run(traffic.Iperf.setup(machines, server="node4", interface="wwan0", clients=["pacify"]))
    install, kwargs = machines.calls[0]
    assert "apt-get install -y iperf3" in install[-1]
    assert kwargs["resources"] == ["pacify"]
    assert kwargs["become"] is True


def test_shared_tunnel_addresses_are_rejected(ansible):
    ue = Containers({"a": "10.45.0.2", "b": "10.45.0.2"})
    with pytest.raises(ValueError, match="share a tunnel address"):
        asyncio.run(traffic.Iperf.setup(ue, server="node4", interface="oaitun_ue1", clients=["a", "b"]))


def test_run_starts_a_server_on_a_fresh_port_then_the_client(ansible):
    ue = Containers({"sim_ue": "10.45.0.2"})

    async def scenario():
        iperf = await traffic.Iperf.setup(ue, server="node4", interface="oaitun_ue1", clients=["sim_ue"])
        first = await iperf.run("sim_ue", "downlink", 5)
        second = await iperf.run("sim_ue", "uplink", 5)
        return first, second

    first, second = asyncio.run(scenario())
    servers = [kwargs["command"] for kind, kwargs in ansible if kind == "command"]
    assert [command[command.index("-p") + 1] for command in servers] == ["5201", "5202"]
    assert first["command"][-1] == "-R"
    assert "-R" not in second["command"]
    assert first["command"][first["command"].index("-B") + 1] == "10.45.0.2"


def test_unknown_server_node(ansible):
    with pytest.raises(ValueError, match="Unknown server node"):
        asyncio.run(traffic.Iperf.setup(Containers({}), server="node9", interface="x", clients=[]))
