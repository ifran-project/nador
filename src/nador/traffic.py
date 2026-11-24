"""iperf3 between UEs and a server node: install it, find the UEs' tunnel addresses, run tests.

```python
iperf = await Iperf.setup(ue, server="node4", interface="oaitun_ue1", clients=["sim_ue", "sim_ue2"])
await iperf.serve(5201)
report = await iperf.client("sim_ue", "downlink", 20, 5201)
```

``clients`` are UE containers when the manager is an IFRAN ``UEManagement``, or UE machines
reached over SSH when it is a ``ProvisioningManagement`` (physical UEs, such as USB modems).

Commands sent to UEs are plain argument lists with no spaces inside an argument and no shell:
IFRAN runs container commands through ``kubectl exec`` as one space-joined string, so
``sh -c "<script>"`` would be split apart.
"""

import ipaddress
import json
from contextlib import suppress
from itertools import cycle
from pathlib import Path
from typing import Any, Literal

from ifran.deployment import ProvisioningManagement
from ifran.utils.ansible import get_ansible_inventory_line, run_ansible_command, run_ansible_playbook

# Finds an installed iperf3 without a shell; prints nothing when it is missing.
FIND = ["find", "/usr", "-name", "iperf3", "-type", "f"]
INSTALL = (["apt-get", "update"], ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y", "iperf3"])
# Ports for run(), shared by every Iperf of the notebook: a one-shot server idles up to 60 s on its port, so a new
# Iperf object must not start again from the first port.
PORTS = cycle(range(5201, 5301))


class Iperf:
    """iperf3 tests between UEs and one server node of the deployment."""

    def __init__(self, manager: Any, *, server: str, interface: str, ue_ips: dict[str, str], server_ip: str) -> None:
        model = manager.deployment_model
        self.manager = manager
        self.server = server
        self.interface = interface
        self.ue_ips = ue_ips
        self.server_ip = server_ip
        self._inventory = get_ansible_inventory_line(server, model.nodes[server])
        self._runner = str(Path(model.params.ansible.runner_path) / "iperf-server")

    @classmethod
    async def setup(cls, manager: Any, *, server: str, interface: str, clients: list[str]) -> "Iperf":
        """Install iperf3 on the server and on clients that lack it; find each client's address."""
        model = manager.deployment_model
        if server not in model.nodes:
            raise ValueError(f"Unknown server node {server!r}; nodes: {sorted(model.nodes)}")
        node = model.nodes[server]
        iperf = cls(manager, server=server, interface=interface, ue_ips={}, server_ip=node.ssh_info.hostname or server)
        await run_ansible_playbook(
            playbook=[
                {
                    "name": "Install iperf3 on the server",
                    "hosts": server,
                    "gather_facts": False,
                    "become": True,
                    "tasks": [
                        {"ansible.builtin.apt": {"name": "iperf3", "update_cache": True, "cache_valid_time": 3600}}
                    ],
                }
            ],
            inventory=iperf._inventory,
            runner_path=iperf._runner,
            quiet=True,
        )
        # Install only where missing, so repeated setups are quick and UEs without internet that have it work.
        missing = [name for name, found in (await iperf._run(FIND, clients)).items() if not found.strip()]
        for command in INSTALL if missing else ():
            await iperf._run(command, missing)
        iperf.ue_ips = await iperf._addresses(clients)
        return iperf

    async def _run(self, command: list[str], clients: list[str]) -> dict[str, str]:
        if isinstance(self.manager, ProvisioningManagement):
            output = await self.manager.run_command(command, resources=clients, become=True, quiet=True, flatten=False)
        else:
            output = await self.manager.run_command(command, components=clients, quiet=True, flatten=False)
        if not isinstance(output, dict):
            raise TypeError(f"Expected per-client output, got {type(output).__name__}")
        return output

    async def _addresses(self, clients: list[str]) -> dict[str, str]:
        output = await self._run(["ip", "-j", "-4", "addr", "show", "dev", self.interface], clients)
        addresses = {}
        for client in clients:
            ipv4 = [
                entry["local"]
                for device in json.loads(output[client])
                for entry in device["addr_info"]
                if entry["family"] == "inet"
            ]
            if len(ipv4) != 1:
                raise RuntimeError(f"{client}: expected one IPv4 address on {self.interface}, found {ipv4}")
            addresses[client] = str(ipaddress.IPv4Address(ipv4[0]))
        if len(set(addresses.values())) != len(addresses):
            raise ValueError(f"UEs share a tunnel address: {addresses}")
        return addresses

    async def serve(self, port: int) -> None:
        """Start a one-shot iperf3 server on ``port``; it exits after one test or 60 idle seconds."""
        await run_ansible_command(
            command=["iperf3", "-s", "-D", "-1", "--idle-timeout", "60", "-B", self.server_ip, "-p", str(port), "-J"],
            hosts=self.server,
            inventory=self._inventory,
            runner_path=self._runner,
            quiet=True,
        )

    async def client(
        self, client: str, direction: Literal["uplink", "downlink"], duration: int, port: int
    ) -> dict[str, Any]:
        """Run the UE side for ``duration`` seconds and return iperf3's JSON report.

        A failed test returns a report with an ``error`` key, so it can be saved before you act on it.
        """
        if direction not in ("uplink", "downlink"):
            raise ValueError("direction must be 'uplink' or 'downlink'")
        # iperf3 exits non-zero when a test fails and the UE manager then drops its output, so the
        # report goes to a file (iperf3 appends to it, hence the rm) and is read back either way.
        report = f"/tmp/nador-iperf-{port}.json"  # noqa: S108 - on the UE, rewritten by each test
        await self._run(["rm", "-f", report], [client])
        with suppress(RuntimeError):  # a failed test: its report says why
            await self._run(
                [
                    *("iperf3", "-c", self.server_ip, "-B", self.ue_ips[client], "--bind-dev", self.interface),
                    *("-p", str(port), "-t", str(duration), "--connect-timeout", "10000"),
                    *("-J", "--get-server-output", "--logfile", report),
                    *(["-R"] if direction == "downlink" else []),
                ],
                [client],
            )
        try:
            output = (await self._run(["cat", report], [client]))[client]
            return json.loads(output)
        except (RuntimeError, json.JSONDecodeError) as exc:
            return {"error": f"iperf3 left no report: {exc}"}

    async def run(self, client: str, direction: Literal["uplink", "downlink"], duration: int) -> dict[str, Any]:
        """Start a server on a fresh port, then run the client against it."""
        port = next(PORTS)  # not used by the last 99 run() calls of any Iperf
        await self.serve(port)
        return await self.client(client, direction, duration, port)
