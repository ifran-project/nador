"""Run a handover on the testbed: trigger it on the CU-CP, wait, read the logs, analyze them, and ping from the UE."""

import asyncio
import json
import shlex

from ifran.deployment import ProvisioningManagement, RANManagement
from ifran.utils.k8s import build_k8s_safe

from nador.handover import oai, ocudu
from nador.handover.report import HandoverReport

__all__ = ["run_handover"]

KUBECONFIG = "/etc/kubernetes/admin.conf"
# Logs read after the trigger cover the wait and this margin before it (trigger and pod listing by Ansible).
LOG_MARGIN_S = 10
# WebSocket client run with python3 on the Kubernetes master (standard library only): sends one text message to
# ws://<host>:<port>/ and prints the first text message received (remote control server of the OCUDU CU-CP).
WS_CLIENT = """
import base64, os, socket, struct, sys
host, port, message = sys.argv[1], int(sys.argv[2]), sys.argv[3].encode()
sock = socket.create_connection((host, port), timeout=10)
key = base64.b64encode(os.urandom(16)).decode()
sock.sendall((f"GET / HTTP/1.1\\r\\nHost: {host}:{port}\\r\\nUpgrade: websocket\\r\\nConnection: Upgrade\\r\\n"
              f"Sec-WebSocket-Key: {key}\\r\\nSec-WebSocket-Version: 13\\r\\n\\r\\n").encode())
data = b""
def read(size):
    global data
    while len(data) < size:
        chunk = sock.recv(4096)
        if not chunk:
            sys.exit("connection closed by the server")
        data += chunk
    out, data = data[:size], data[size:]
    return out
while b"\\r\\n\\r\\n" not in data:
    chunk = sock.recv(4096)
    if not chunk:
        sys.exit("connection closed by the server")
    data += chunk
head, data = data.split(b"\\r\\n\\r\\n", 1)
if b" 101 " not in head.split(b"\\r\\n")[0]:
    sys.exit("handshake refused: " + head.decode(errors="replace"))
mask = os.urandom(4)
size = len(message)
header = bytes([0x81, 0x80 | size]) if size < 126 else bytes([0x81, 0x80 | 126]) + struct.pack("!H", size)
sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(message)))
while True:
    first, second = read(2)
    size = second & 0x7F
    if size == 126:
        size = struct.unpack("!H", read(2))[0]
    elif size == 127:
        size = struct.unpack("!Q", read(8))[0]
    payload = read(size)
    if first & 0x0F == 1:
        print(payload.decode())
        break
    if first & 0x0F == 8:
        sys.exit("connection closed by the server")
"""


async def _pods(nodes: ProvisioningManagement, master: str, namespace: str) -> dict[str, dict[str, int]]:
    """Restart count of each container of each pod of the namespace: {pod: {container: count}}."""
    out = await nodes.run_node_command(
        command=["kubectl", "--kubeconfig", KUBECONFIG, "-n", namespace, "get", "pods", "-o", "json"],
        nodes=[master],
        become=True,
        quiet=True,
    )
    return {
        pod["metadata"]["name"]: {c["name"]: c["restartCount"] for c in pod["status"].get("containerStatuses", [])}
        for pod in json.loads(out)["items"]
    }


def _main_log(containers: dict[str, str], main: tuple[str, ...]) -> str:
    """Log of the container running the RAN function (the sidecars are left out)."""
    return "\n".join(log for name, log in containers.items() if name in main)


async def _trigger_oai(ran: RANManagement, ue: int, cucp: str, telnet_port: int) -> tuple[str, str]:
    """Send ci trigger_f1_ho to the OAI CU-CP telnet server: (command, answer)."""
    answer = await ran.run_command(
        command=["bash", "-c", shlex.quote(oai.trigger_command(ue, telnet_port))],
        components=[cucp],
        quiet=True,
        flatten=False,
    )
    return f"ci trigger_f1_ho {ue}", str(answer[cucp]).strip()  # type: ignore[index]


async def _trigger_ocudu(
    ran: RANManagement,
    rnti: int | str,
    *,
    cucp: str,
    source: str,
    target: str,
    nodes: ProvisioningManagement,
    master: str,
    pod: str,
) -> tuple[str, str, int]:
    """Send trigger_handover to the remote control server of the OCUDU CU-CP: (request, answer, target PCI)."""
    remote_control = (ran.deployment_model.ran.components[cucp].advanced_config or {}).get("remote_control") or {}  # type: ignore[union-attr]
    if not remote_control.get("enabled"):
        raise ValueError(
            f"remote control of {cucp} disabled: set advanced_config.remote_control "
            "{enabled: true, bind_addr: 0.0.0.0, port: 8001} in the deployment model of the CU-CP"
        )
    configs = [ran.metamodel.ran.components[name].config for name in (source, target)]  # type: ignore[union-attr]
    request = json.dumps(
        ocudu.trigger_request(
            configs[0].physicalCellId,
            rnti,
            configs[1].physicalCellId,
            configs[1].plmnList[0].plmnId.plmnId,
            configs[1].tac,
        )
    )
    namespace = ran.deployment_model.ran.components[cucp].namespace
    address = await nodes.run_node_command(
        command=[
            "kubectl",
            "--kubeconfig",
            KUBECONFIG,
            "-n",
            namespace,
            "get",
            "pod",
            pod,
            "-o",
            "jsonpath={.status.podIP}",
        ],
        nodes=[master],
        become=True,
        quiet=True,
    )
    answer = await nodes.run_node_command(
        command=["python3", "-c", WS_CLIENT, str(address).strip(), str(remote_control.get("port", 8001)), request],
        nodes=[master],
        become=True,
        quiet=True,
    )
    return request, str(answer).strip(), configs[1].physicalCellId


async def _read_restarted(
    du_logs: dict[str, str],
    before: dict[str, dict[str, int]],
    after: dict[str, dict[str, int]],
    *,
    charts: dict[str, str],
    main: tuple[str, ...],
    nodes: ProvisioningManagement,
    master: str,
    namespace: str,
) -> dict[str, int]:
    """Restarts of the DU containers during the handover; their previous log is put before their current one."""
    restarts: dict[str, int] = {}
    for name in list(du_logs):  # the logs are updated in the loop
        for pod, containers in after.items():
            if not pod.startswith(charts[name] + "-"):
                continue
            for container, count in containers.items():
                increase = count - before.get(pod, {}).get(container, 0)
                if container not in main or increase <= 0:
                    continue
                restarts[f"{name}/{container}"] = increase
                # the handover happened in the previous container: read its log too
                previous = await nodes.run_node_command(
                    command=[
                        "kubectl",
                        "--kubeconfig",
                        KUBECONFIG,
                        "-n",
                        namespace,
                        "logs",
                        pod,
                        "-c",
                        container,
                        "--previous",
                    ],
                    nodes=[master],
                    become=True,
                    quiet=True,
                )
                du_logs[name] = previous + "\n" + du_logs[name]
    return restarts


async def run_handover(  # noqa: C901
    ran: RANManagement,
    ue: int | str,
    *,
    source: str,
    target: str,
    cucp: str = "cucp",
    telnet_port: int = 9091,
    wait_s: float = 15,
    ue_manager: ProvisioningManagement | None = None,
    ue_name: str | None = None,
    ue_interface: str = "wwan0",
    ping_address: str = "11.1.1.1",
    quiet: bool = False,
    explain: bool = True,
) -> HandoverReport:
    """
    Hand a UE over from the DU source to the DU target, then check each step of the handover in the logs.

    The RAN implementation of the CU-CP decides how:

    - OAI: ue is the CU UE ID (1, 2... in the order of attachment); ``ci trigger_f1_ho <ue>`` goes to the CU-CP
      telnet server (telnet_port, ``--telnetsrv.listenport`` of the CU-CP);
    - OCUDU: ue is the C-RNTI of the UE in the source cell (``0x4601``, as in the CU-CP log); the remote command
      trigger_handover (source PCI, ue, target PCI, PLMN and TAC from the metamodel) goes to the remote control server
      of the CU-CP (advanced_config.remote_control of the CU-CP: enabled, port).

    After wait_s seconds, the logs of the CU-CP and of both DUs are read (OCUDU: from just before the trigger on; with
    the previous log of a DU container that restarted meanwhile) and analyzed. With ue_manager and ue_name (the COTS
    UE handed over), the UE then pings ping_address over ue_interface. The report is printed unless quiet (with
    explain, what each step means and the log line that proves it), and returned.
    """
    model = ran.deployment_model
    implementation = str(model.ran.components[cucp].implementation)
    if implementation not in ("oai", "ocudu"):
        raise ValueError(f"handover of {implementation!r} RAN not supported (oai, ocudu)")
    module = oai if implementation == "oai" else ocudu
    master = model.params.k8s.master_node
    namespace = model.ran.components[target].namespace
    nodes = ProvisioningManagement(preceding=ran)
    charts = {name: build_k8s_safe(f"{implementation}-{name}") for name in (cucp, source, target)}
    before = await _pods(nodes, master, namespace)

    if implementation == "oai":
        command, answer = await _trigger_oai(ran, int(ue), cucp, telnet_port)
    else:
        pod = next((p for p in before if p.startswith(charts[cucp] + "-")), None)
        if pod is None:
            raise RuntimeError(f"no pod of {charts[cucp]} in namespace {namespace}")
        command, answer, target_pci = await _trigger_ocudu(
            ran, ue, cucp=cucp, source=source, target=target, nodes=nodes, master=master, pod=pod
        )
    await asyncio.sleep(wait_s)

    after = await _pods(nodes, master, namespace)
    since = int(wait_s) + LOG_MARGIN_S if implementation == "ocudu" else 3600
    logs = await ran.get_logs(components=[cucp, source, target], since_seconds=since, quiet=True, flatten=False)
    du_logs = {name: _main_log(logs[name], module.CONTAINERS) for name in (source, target)}  # type: ignore[index]
    restarts = await _read_restarted(
        du_logs, before, after, charts=charts, main=module.CONTAINERS, nodes=nodes, master=master, namespace=namespace
    )

    cucp_log = _main_log(logs[cucp], module.CONTAINERS)  # type: ignore[index]
    if implementation == "oai":
        report = oai.analyze_oai_handover(int(ue), source, target, cucp_log, du_logs)
    else:
        report = ocudu.analyze_ocudu_handover(
            ue, source, target, cucp_log, du_logs, target_pci=target_pci, answer=answer
        )
    report.command, report.command_answer = command, answer
    report.restarts = restarts
    report.aftermath += [f"{name} restarted {count} time(s)" for name, count in restarts.items()]
    if ue_manager is not None and ue_name is not None:
        try:
            out = await ue_manager.run_ue_command(
                command=["sh", "-c", f"ping -c 3 -W 2 -I {ue_interface} {ping_address} | tail -2"],
                ues=[ue_name],
                quiet=True,
                flatten=False,
            )
            report.ping = f"{ue_name}: " + " / ".join(out[ue_name].strip().splitlines())  # type: ignore[index]
        except RuntimeError:
            report.ping = f"{ue_name}: FAILED (no reply or no address on {ue_interface})"
    if not quiet:
        print(report.to_text(explain=explain))  # noqa: T201
    return report
