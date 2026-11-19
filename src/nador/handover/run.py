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
# Seconds kubectl attach stays attached to the OCUDU CU-CP console to write the command (it does not exit by itself).
ATTACH_TIMEOUT_S = 5


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
    source: str,
    target: str,
    nodes: ProvisioningManagement,
    master: str,
    pod: str,
    container: str,
) -> tuple[str, str]:
    """Write ho <serving PCI> <C-RNTI> <target PCI> on the OCUDU CU-CP console with kubectl attach."""
    pcis = [ran.metamodel.ran.components[name].config.physicalCellId for name in (source, target)]  # type: ignore[union-attr]
    command = ocudu.trigger_command(pcis[0], rnti, pcis[1])
    namespace = ran.deployment_model.ran.components[target].namespace
    attach = (
        f"printf '%s\\n' {shlex.quote(command)} | timeout {ATTACH_TIMEOUT_S} kubectl --kubeconfig {KUBECONFIG} "
        f"-n {namespace} attach -i -q {pod} -c {container} 2>&1; true"
    )
    answer = await nodes.run_node_command(command=["sh", "-c", attach], nodes=[master], become=True, quiet=True)
    return command, str(answer).strip()


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
    - OCUDU: ue is the C-RNTI of the UE in the source cell (``0x4601``, as in the CU-CP log); ``ho <source PCI> <ue>
      <target PCI>`` goes to the CU-CP console (the PCIs come from the metamodel).

    After wait_s seconds, the logs of the CU-CP and of both DUs are read (with the previous log of a DU container that
    restarted meanwhile) and analyzed. With ue_manager and ue_name (the COTS UE handed over), the UE then pings
    ping_address over ue_interface. The report is printed unless quiet (with explain, what each step means and the
    log line that proves it), and returned.
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
        container = next(c for c in before[pod] if c in ocudu.CONTAINERS)  # cucp, or cu for a CU with CU-UP
        command, answer = await _trigger_ocudu(
            ran, ue, source=source, target=target, nodes=nodes, master=master, pod=pod, container=container
        )
    await asyncio.sleep(wait_s)

    after = await _pods(nodes, master, namespace)
    logs = await ran.get_logs(components=[cucp, source, target], quiet=True, flatten=False)
    du_logs = {name: _main_log(logs[name], module.CONTAINERS) for name in (source, target)}  # type: ignore[index]
    restarts = await _read_restarted(
        du_logs, before, after, charts=charts, main=module.CONTAINERS, nodes=nodes, master=master, namespace=namespace
    )

    cucp_log = _main_log(logs[cucp], module.CONTAINERS)  # type: ignore[index]
    if implementation == "oai":
        report = oai.analyze_oai_handover(int(ue), source, target, cucp_log, du_logs)
    else:
        report = ocudu.analyze_ocudu_handover(ue, source, target, cucp_log, du_logs)
    report.command, report.command_answer = command, answer
    report.restarts = restarts
    report.aftermath += [f"{name} restarted {count} time(s)" for name, count in restarts.items()]
    if implementation == "ocudu" and not report.step("decision").ok and "stdin" in answer:
        report.notes.append(
            "The CU-CP container must run with stdin: true for kubectl attach to reach its console: select the nador "
            "ocudu-cu_cp chart (custom_chart_path).",
        )
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
