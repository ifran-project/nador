"""The iperf shortcut, with IFRAN's managers and Ansible replaced by recorders."""

import json
from types import SimpleNamespace

import pytest
from ifran.models.provisioning import SSHInfo

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
        node = SimpleNamespace(name="node4", ssh_info=SSHInfo(hostname="10.0.0.4"))
        self.deployment_model = SimpleNamespace(
            nodes={"node4": node}, params=SimpleNamespace(ansible=SimpleNamespace(runner_path="runner"), ssh=SSHInfo())
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
    """Stands in for an IFRAN ProvisioningManagement: commands run over SSH on UE machines or nodes.

    Built with ``preceding``, it is the manager that reaches the server node; its commands go to ``SERVER``.
    """

    def __init__(self, outputs: dict[str, str] | None = None, *, preceding=None, **kwargs):
        super().__init__(outputs or {}, **kwargs)
        if preceding is not None:
            self.deployment_model = preceding.deployment_model

    async def run_node_command(self, command, nodes, **kwargs):
        SERVER.append((command, nodes, kwargs))
        if command[0] == "find":
            return "/usr/bin/iperf3" if any("install" in command for command, _, _ in SERVER) else ""
        return ""


SERVER = []


@pytest.fixture
def server(monkeypatch):
    """The commands run on the server node, as (command, nodes, options)."""
    SERVER.clear()
    monkeypatch.setattr(traffic, "ProvisioningManagement", Machines)
    return SERVER


async def test_setup_installs_the_server_and_finds_ue_addresses(server):
    ue = Containers({"sim_ue": "10.45.0.2", "sim_ue2": "10.45.0.3"})
    iperf = await traffic.Iperf.setup(ue, server="node4", interface="oaitun_ue1", clients=["sim_ue", "sim_ue2"])
    assert iperf.server_ip == "10.0.0.4"
    assert iperf.ue_ips == {"sim_ue": "10.45.0.2", "sim_ue2": "10.45.0.3"}
    assert [command[-1] for command, _, _ in server[:3]] == ["f", "update", "iperf3"]  # find, then install
    assert all(nodes == ["node4"] and kwargs["become"] for _, nodes, kwargs in server)
    assert not any("apt-get" in command for command, _ in ue.calls)  # already installed
    assert all("components" in kwargs for _, kwargs in ue.calls)


async def test_setup_leaves_an_installed_server_alone(server):
    ue = Containers({"sim_ue": "10.45.0.2"})
    await traffic.Iperf.setup(ue, server="node4", interface="oaitun_ue1", clients=["sim_ue"])
    await traffic.Iperf.setup(ue, server="node4", interface="oaitun_ue1", clients=["sim_ue"])
    assert [command[0] for command, _, _ in server] == ["find", "apt-get", "env", "find"]


async def test_setup_installs_iperf_in_containers_that_lack_it(server):
    ue = Containers({"sim_ue": "10.45.0.2"}, installed=False)
    await traffic.Iperf.setup(ue, server="node4", interface="tun_srsue", clients=["sim_ue"])
    assert ue.installed == {"sim_ue"}


async def test_ue_machines_get_iperf_installed_with_sudo(server):
    machines = Machines({"pacify": "10.45.0.9"}, installed=False)
    await traffic.Iperf.setup(machines, server="node4", interface="wwan0", clients=["pacify"])
    install, kwargs = next((command, kwargs) for command, kwargs in machines.calls if "install" in command)
    assert install[-1] == "iperf3"
    assert kwargs["resources"] == ["pacify"]
    assert kwargs["become"] is True


async def test_ues_sharing_a_tunnel_address_are_refused(server):
    ue = Containers({"a": "10.45.0.2", "b": "10.45.0.2"})
    with pytest.raises(ValueError, match="share a tunnel address"):
        await traffic.Iperf.setup(ue, server="node4", interface="oaitun_ue1", clients=["a", "b"])


async def test_run_starts_a_server_on_a_fresh_port_then_the_client(server):
    ue = Containers({"sim_ue": "10.45.0.2"})

    iperf = await traffic.Iperf.setup(ue, server="node4", interface="oaitun_ue1", clients=["sim_ue"])
    first = await iperf.run("sim_ue", "downlink", 5)
    second = await iperf.run("sim_ue", "uplink", 5)
    again = await traffic.Iperf.setup(ue, server="node4", interface="oaitun_ue1", clients=["sim_ue"])
    await again.run("sim_ue", "uplink", 5)  # a new Iperf: the first server may still idle on its port
    servers = [command for command, _, _ in server if command[0] == "iperf3"]
    ports = [int(command[command.index("-p") + 1]) for command in servers]
    assert len(set(ports)) == 3
    assert all(5201 <= port <= 5300 for port in ports)
    assert first["command"][-1] == "-R"
    assert "-R" not in second["command"]
    assert first["command"][first["command"].index("-B") + 1] == "10.45.0.2"


async def test_a_failed_test_still_returns_its_report(server):
    ue = Containers({"sim_ue": "10.45.0.2"}, iperf_error="unable to connect to server: Connection refused")
    iperf = await traffic.Iperf.setup(ue, server="node4", interface="tun", clients=["sim_ue"])
    assert await iperf.run("sim_ue", "uplink", 5) == {"error": "unable to connect to server: Connection refused"}


async def test_an_unknown_server_node_is_refused(server):
    with pytest.raises(ValueError, match="Unknown server node"):
        await traffic.Iperf.setup(Containers({}), server="node9", interface="x", clients=[])
