"""The iperf shortcut, with IFRAN's managers and Ansible replaced by recorders."""

import json
from types import SimpleNamespace

import pytest

from nador import traffic


def address(ip):
    return json.dumps([{"addr_info": [{"family": "inet", "local": ip}, {"family": "inet6", "local": "fe80::1"}]}])


class Containers:
    """Stands in for an IFRAN UEManagement: commands run in UE containers through kubectl exec.

    Like IFRAN, it cannot pass an argument containing a space, and a command that exits non-zero
    raises RuntimeError and loses its output.
    """

    def __init__(self, outputs, installed=True, iperf_error=None):
        self.calls = []
        self.outputs = outputs
        self.installed = set(outputs) if installed else set()
        self.iperf_error = iperf_error
        self.files = {}
        node = SimpleNamespace(name="node4", ssh_info=SimpleNamespace(hostname="10.0.0.4"))
        self.deployment_model = SimpleNamespace(
            nodes={"node4": node}, params=SimpleNamespace(ansible=SimpleNamespace(runner_path="runner"))
        )

    async def run_command(self, command, **kwargs):
        assert all(" " not in argument for argument in command), f"IFRAN would split {command}"
        self.calls.append((command, kwargs))
        targets = kwargs.get("components") or kwargs.get("resources")
        program = command[0] if command[0] != "env" else command[2]
        if program == "find":
            return {name: "/usr/bin/iperf3" if name in self.installed else "" for name in targets}
        if program == "apt-get" and "install" in command:
            self.installed |= set(targets)
        if program == "ip":
            return {name: address(self.outputs[name]) for name in targets}
        if program == "rm":
            self.files.pop(command[-1], None)
        if program == "iperf3":
            if targets[0] not in self.installed:
                raise RuntimeError(f"UE component {targets[0]!r} Helm run_cmd not successful.")
            log = command[command.index("--logfile") + 1]
            self.files[log] = json.dumps(
                {"error": self.iperf_error} if self.iperf_error else {"end": {}, "command": command}
            )
            if self.iperf_error:
                raise RuntimeError(f"UE component {targets[0]!r} Helm run_cmd not successful.")
        if program == "cat":
            if command[-1] not in self.files:
                raise RuntimeError(f"UE component {targets[0]!r} Helm run_cmd not successful.")
            return {targets[0]: self.files[command[-1]]}
        return dict.fromkeys(targets, "")


class Machines(Containers):
    """Stands in for an IFRAN ProvisioningManagement: commands run over SSH on UE machines."""


@pytest.fixture
def playbooks(monkeypatch):
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


async def test_setup_installs_the_server_and_finds_ue_addresses(playbooks):
    ue = Containers({"sim_ue": "10.45.0.2", "sim_ue2": "10.45.0.3"})
    iperf = await traffic.Iperf.setup(ue, server="node4", interface="oaitun_ue1", clients=["sim_ue", "sim_ue2"])
    assert iperf.server_ip == "10.0.0.4"
    assert iperf.ue_ips == {"sim_ue": "10.45.0.2", "sim_ue2": "10.45.0.3"}
    assert playbooks[0][0] == "playbook"
    assert not any("apt-get" in command for command, _ in ue.calls)  # already installed
    assert all("components" in kwargs for _, kwargs in ue.calls)


async def test_setup_installs_iperf_in_containers_that_lack_it(playbooks):
    ue = Containers({"sim_ue": "10.45.0.2"}, installed=False)
    await traffic.Iperf.setup(ue, server="node4", interface="tun_srsue", clients=["sim_ue"])
    assert ue.installed == {"sim_ue"}


async def test_ue_machines_get_iperf_installed_with_sudo(playbooks):
    machines = Machines({"pacify": "10.45.0.9"}, installed=False)
    await traffic.Iperf.setup(machines, server="node4", interface="wwan0", clients=["pacify"])
    install, kwargs = next((command, kwargs) for command, kwargs in machines.calls if "install" in command)
    assert install[-1] == "iperf3"
    assert kwargs["resources"] == ["pacify"]
    assert kwargs["become"] is True


async def test_ues_sharing_a_tunnel_address_are_refused(playbooks):
    ue = Containers({"a": "10.45.0.2", "b": "10.45.0.2"})
    with pytest.raises(ValueError, match="share a tunnel address"):
        await traffic.Iperf.setup(ue, server="node4", interface="oaitun_ue1", clients=["a", "b"])


async def test_run_starts_a_server_on_a_fresh_port_then_the_client(playbooks):
    ue = Containers({"sim_ue": "10.45.0.2"})

    iperf = await traffic.Iperf.setup(ue, server="node4", interface="oaitun_ue1", clients=["sim_ue"])
    first = await iperf.run("sim_ue", "downlink", 5)
    second = await iperf.run("sim_ue", "uplink", 5)
    again = await traffic.Iperf.setup(ue, server="node4", interface="oaitun_ue1", clients=["sim_ue"])
    await again.run("sim_ue", "uplink", 5)  # a new Iperf: the first server may still idle on its port
    servers = [kwargs["command"] for kind, kwargs in playbooks if kind == "command"]
    ports = [int(command[command.index("-p") + 1]) for command in servers]
    assert len(set(ports)) == 3
    assert all(5201 <= port <= 5300 for port in ports)
    assert first["command"][-1] == "-R"
    assert "-R" not in second["command"]
    assert first["command"][first["command"].index("-B") + 1] == "10.45.0.2"


async def test_a_failed_test_still_returns_its_report(playbooks):
    ue = Containers({"sim_ue": "10.45.0.2"}, iperf_error="unable to connect to server: Connection refused")
    iperf = await traffic.Iperf.setup(ue, server="node4", interface="tun", clients=["sim_ue"])
    assert await iperf.run("sim_ue", "uplink", 5) == {"error": "unable to connect to server: Connection refused"}


async def test_an_unknown_server_node_is_refused(playbooks):
    with pytest.raises(ValueError, match="Unknown server node"):
        await traffic.Iperf.setup(Containers({}), server="node9", interface="x", clients=[])
