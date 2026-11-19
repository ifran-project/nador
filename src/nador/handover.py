"""
Trigger an OAI F1 handover from the CU-CP telnet server and check it, step by step, in the CU-CP and DU logs.

The handover of a UE (CU UE ID) from a source DU to a target DU goes through these steps, each visible in the logs:

1. the CU-CP triggers it ("Handover triggered for UE <id>/RNTI <old> towards cell <cell>");
2. the target DU prepares the UE with a dedicated (CFRA) preamble ("Added new CFRA process for UE RNTI <new>");
3. the target DU receives this preamble ("TC-RNTI <new>: initiating RA procedure for RAPID <preamble>");
4. it sends the random access response ("(rnti 0x<new>) CFRA procedure succeeded!": OAI logs it when it sends the
   response, not when the UE receives it); if the UE sends the same preamble again, the target DU ignores it
   ("ignoring RAPID <preamble>"): the UE did not decode the response;
5. the UE confirms on the target cell ("(cellID <cell>, UE ID <id> ...) Received RRCReconfigurationComplete").

After a failure, the UE re-establishes (on the target or the source cell), the CU-CP may roll the handover back,
or the UE attaches again; a DU may also abort (OAI before 2026.w45: "Could not encode CellGroup" when a UE
re-establishes with SRB1 only on the target DU) and restart, in which case its previous logs are read.
"""

import asyncio
import json
import re
import shlex
from dataclasses import dataclass, field

from ifran.deployment import ProvisioningManagement, RANManagement
from ifran.utils.k8s import build_k8s_safe

__all__ = ["HandoverReport", "analyze_handover", "run_handover"]

# Containers of the OAI charts that do not run the gNB/DU/CU (nador metrics sidecar and init containers).
SIDECARS = ("metrics-parser", "prepare-du-metrics", "prepare-cu-metrics", "copy-yq")
KUBECONFIG = "/etc/kubernetes/admin.conf"
# Lines read at the end of a DU log to tell which UEs it still serves (MAC statistics every 1.28 s).
STATS_TAIL_LINES = 300


@dataclass
class HandoverReport:
    """Result of a handover: steps reached, diagnosis, and what happened next."""

    ue_id: int
    source: str
    target: str
    success: bool = False
    diagnosis: str = ""
    steps: list[tuple[str, bool, str]] = field(default_factory=list)
    aftermath: list[str] = field(default_factory=list)
    target_cell: int | None = None
    rntis: dict[str, str] = field(default_factory=dict)
    preamble: int | None = None
    ue_location: dict[str, str] = field(default_factory=dict)
    restarts: dict[str, int] = field(default_factory=dict)
    telnet_answer: str = ""
    ping: str | None = None

    def to_text(self) -> str:
        """Human-readable report."""
        out = [
            f"Handover of CU UE ID {self.ue_id}: {self.source} -> {self.target}: "
            + ("SUCCESS" if self.success else "FAILED"),
            f"  {self.diagnosis}",
            "Steps:",
        ]
        out += [f"  [{'x' if ok else ' '}] {name}{': ' + detail if detail else ''}" for name, ok, detail in self.steps]
        if self.aftermath:
            out.append("Then:")
            out += [f"  - {event}" for event in self.aftermath]
        if self.restarts:
            out.append("Restarted containers: " + ", ".join(f"{k} (+{v})" for k, v in self.restarts.items()))
        out.append("UE now:")
        out += [f"  {du}: {line}" for du, line in self.ue_location.items()]
        if self.ping is not None:
            out.append(f"Ping: {self.ping}")
        return "\n".join(out)


def _after_last(lines: list[str], needle: str) -> list[str]:
    """Lines after the last line containing needle (all lines if absent)."""
    for index in range(len(lines) - 1, -1, -1):
        if needle in lines[index]:
            return lines[index:]
    return []


def _cucp_steps(report: HandoverReport, lines: list[str]) -> dict:  # noqa: C901 (one branch per log line kind)
    """CU-CP part: trigger, RNTIs, completion, re-establishments, rollback and new attachment of the UE."""
    ue = report.ue_id
    found: dict = {"triggered": False, "acked": False, "complete": None, "events": []}
    rntis: list[str] = []
    triggered = rf"Handover triggered for UE {ue}/RNTI ([0-9a-f]+) towards cell (\d+)/assoc_id \d+/PCI (\d+)"
    updated = rf"UE {ue}: update CU F1AP Context DU UE ID \d+ => \d+ and RNTI ([0-9a-f]+) => ([0-9a-f]+)"
    completed = rf"\(cellID (\d+), UE ID {ue} RNTI ([0-9a-f]+)\) Received RRCReconfigurationComplete"
    for line in lines:
        if m := re.search(triggered, line):
            found["triggered"] = True
            report.rntis["source"] = m.group(1)
            report.target_cell = int(m.group(2))
            rntis.append(m.group(1))
        elif re.search(rf"HO acknowledged: Send reconfiguration for UE {ue}/", line):
            found["acked"] = True
        elif m := re.search(updated, line):
            report.rntis.setdefault("target", m.group(2))
            rntis.append(m.group(2))
        elif m := re.search(completed, line):
            # only the first completion counts, and only if it comes before any re-establishment of the UE
            if found["complete"] is None and not found["events"]:
                found["complete"] = int(m.group(1))
        elif m := re.search(r"Reestablishment RNTI ([0-9a-f]+) req C-RNTI ([0-9a-f]+) physCellId (\d+)", line):
            if m.group(2) in rntis:
                rntis.append(m.group(1))
                found["events"].append(
                    f"re-establishment (RNTI {m.group(1)}, from C-RNTI {m.group(2)}, PCI {m.group(3)})",
                )
        elif re.search(rf"handover for UE {ue}/RNTI \S+ failed, rollback", line):
            found["events"].append("CU-CP: handover failed, rolled back to the source cell")
        elif "without UE context, fallback to RRC setup" in line and found["events"]:
            found["events"].append("CU-CP: re-establishment without UE context, fallback to RRC setup")
        elif re.search(rf"Create UE context: CU UE ID {ue} ", line) and found["triggered"]:
            found["events"].append("UE attached again (new UE context)")
    report.rntis.setdefault("chain", " ".join(rntis))
    return found


def _target_steps(report: HandoverReport, lines: list[str]) -> dict:  # noqa: C901 (one branch per log line kind)
    """Target DU part: CFRA preparation, preamble, response, retransmission of the preamble, abort."""
    found: dict = {"prepared": False, "received": False, "response": False, "retransmissions": 0, "events": []}
    new = report.rntis.get("target")
    if new is None:  # no RNTI from the CU-CP: take the last CFRA process of the target DU
        pattern = r"Added new CFRA process for UE RNTI ([0-9a-f]+)"
        cfra = [m.group(1) for line in lines if (m := re.search(pattern, line))]
        new = cfra[-1] if cfra else None
        if new:
            report.rntis["target"] = new
    if new is None:
        return found
    for line in _after_last(lines, f"Added new CFRA process for UE RNTI {new}"):
        if m := re.search(rf"Added new CFRA process for UE RNTI {new} .*preamble\(s\):\s+(\d+)", line):
            found["prepared"] = True
            report.preamble = int(m.group(1))
        elif re.search(rf"TC-RNTI {new}: initiating RA procedure for RAPID (\d+)", line):
            found["received"] = True
        elif f"(rnti 0x{new}) CFRA procedure succeeded" in line:
            found["response"] = True
        elif found["response"] and re.search(rf"ignoring RAPID {report.preamble}\b", line):
            found["retransmissions"] += 1
        elif "re-establishment with only one LCID" in line:
            found["events"].append(f"{report.target}: re-establishment with SRB1 only refused (OAI 2026.w45 and later)")
        elif "Could not encode CellGroup" in line or "Assertion" in line:
            found["events"].append(f"{report.target}: ABORTED: {line.split(' F ', 1)[-1].strip()}")
    return found


def _ue_location(report: HandoverReport, logs: dict[str, str]) -> None:
    """Last MAC statistics line of the UE on each DU (among its RNTIs), or 'not served'."""
    rntis = set(report.rntis.get("chain", "").split()) | {r for k, r in report.rntis.items() if k != "chain"}
    for du, text in logs.items():
        tail = text.splitlines()[-STATS_TAIL_LINES:]
        lines = [line for line in tail if re.search(rf"UE RNTI ([0-9a-f]+) CU-UE-ID {report.ue_id}\b", line)]
        lines = [line for line in lines if re.search(r"UE RNTI ([0-9a-f]+)", line).group(1) in rntis] or lines
        report.ue_location[du] = lines[-1].split(" F ", 1)[-1].strip() if lines else "not served"


def analyze_handover(ue_id: int, source: str, target: str, cucp_log: str, du_logs: dict[str, str]) -> HandoverReport:
    """Analyze the logs of a handover (CU-CP log from the trigger command on, and the logs of both DUs)."""
    report = HandoverReport(ue_id=ue_id, source=source, target=target)
    cucp = _cucp_steps(report, _after_last(cucp_log.splitlines(), f"trigger_f1_ho {ue_id}"))
    du = _target_steps(report, du_logs.get(target, "").splitlines())
    _ue_location(report, du_logs)

    cell = f" towards cell {report.target_cell}" if report.target_cell is not None else ""
    report.steps = [
        ("CU-CP triggers the handover", cucp["triggered"], f"UE {ue_id}, RNTI {report.rntis.get('source', '?')}{cell}"),
        ("CU-CP sends the handover command", cucp["acked"], ""),
        (
            f"{target} prepares the UE (CFRA)",
            du["prepared"],
            f"RNTI {report.rntis.get('target', '?')}, preamble {report.preamble}" if du["prepared"] else "",
        ),
        (f"{target} receives the dedicated preamble", du["received"], ""),
        (
            f"{target} sends the random access response",
            du["response"],
            f"then the preamble came again {du['retransmissions']} time(s)" if du["retransmissions"] else "",
        ),
        (
            "UE confirms on the target cell (RRCReconfigurationComplete)",
            cucp["complete"] is not None and cucp["complete"] == report.target_cell,
            "",
        ),
    ]
    report.aftermath = cucp["events"] + du["events"]
    report.success = report.steps[-1][1]

    if report.success:
        report.diagnosis = "The UE completed the handover on the target cell."
    elif not cucp["triggered"]:
        report.diagnosis = "The CU-CP did not trigger the handover: check the CU UE ID and the telnet answer."
    elif not du["prepared"]:
        report.diagnosis = f"{target} did not prepare the UE (no CFRA process): UE context setup failed on the target."
    elif not du["received"]:
        report.diagnosis = (
            f"{target} never received the dedicated preamble {report.preamble}: the UE did not execute the "
            "handover command, or its preamble did not reach the target RU."
        )
    elif du["retransmissions"]:
        report.diagnosis = (
            "The UE did not decode the random access response of the target cell: it sent its dedicated "
            "preamble again (downlink of the target cell at the UE)."
        )
    else:
        report.diagnosis = (
            "The UE accessed the target cell, but its RRCReconfigurationComplete never reached the CU-CP "
            "(uplink of the target cell, or the UE lost the link right after the access)."
        )
    return report


def _main_log(containers: dict[str, str]) -> str:
    return "\n".join(log for name, log in containers.items() if name not in SIDECARS)


async def _restart_counts(nodes: ProvisioningManagement, master: str, namespace: str) -> dict[str, dict[str, int]]:
    """Restart count of each container of each pod of the namespace: {pod: {container: count}}."""
    out = await nodes.run_node_command(
        command=["kubectl", "--kubeconfig", KUBECONFIG, "-n", namespace, "get", "pods", "-o", "json"],
        nodes=[master],
        become=True,
        quiet=True,
    )
    pods = json.loads(out)["items"]
    return {
        pod["metadata"]["name"]: {c["name"]: c["restartCount"] for c in pod["status"].get("containerStatuses", [])}
        for pod in pods
    }


async def run_handover(  # noqa: PLR0913
    ran: RANManagement,
    ue_id: int,
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
) -> HandoverReport:
    """
    Trigger the handover of CU UE ID ue_id with the CU-CP telnet server, wait wait_s, then check it in the logs.

    With ue_manager and ue_name (the COTS UE of this CU UE ID), the UE also pings ping_address over ue_interface.
    The telnet answer is read in full before closing: with OAI f80a883, closing earlier crashed the CU-CP.
    """
    model = ran.deployment_model
    master = model.params.k8s.master_node
    namespace = model.ran.components[target].namespace
    nodes = ProvisioningManagement(preceding=ran)
    charts = {name: build_k8s_safe(f"{model.ran.components[name].implementation}-{name}") for name in (source, target)}
    before = await _restart_counts(nodes, master, namespace)

    telnet = (
        f"exec 3<>/dev/tcp/127.0.0.1/{telnet_port}; "
        f'printf "ci trigger_f1_ho {ue_id}\\n" >&3; '
        'while IFS= read -r -t 2 line <&3; do printf "%s\\n" "$line"; done; '
        "exec 3<&-; exec 3>&-"
    )
    answer = await ran.run_command(
        command=["bash", "-c", shlex.quote(telnet)],
        components=[cucp],
        quiet=True,
        flatten=False,
    )
    await asyncio.sleep(wait_s)

    after = await _restart_counts(nodes, master, namespace)
    logs = await ran.get_logs(components=[cucp, source, target], quiet=True, flatten=False)
    du_logs = {name: _main_log(logs[name]) for name in (source, target)}
    restarts: dict[str, int] = {}
    for name in (source, target):
        for pod, containers in after.items():
            if not pod.startswith(charts[name] + "-"):
                continue
            for container, count in containers.items():
                increase = count - before.get(pod, {}).get(container, 0)
                if container in SIDECARS or increase <= 0:
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

    report = analyze_handover(ue_id, source, target, _main_log(logs[cucp]), du_logs)
    report.restarts = restarts
    report.telnet_answer = str(answer[cucp]).strip()
    if restarts:
        report.aftermath += [f"{name} restarted {count} time(s)" for name, count in restarts.items()]
    if ue_manager is not None and ue_name is not None:
        try:
            out = await ue_manager.run_ue_command(
                command=["sh", "-c", f"ping -c 3 -W 2 -I {ue_interface} {ping_address} | tail -2"],
                ues=[ue_name],
                quiet=True,
                flatten=False,
            )
            report.ping = f"{ue_name}: " + " / ".join(out[ue_name].strip().splitlines())
        except RuntimeError:
            report.ping = f"{ue_name}: FAILED (no reply or no address on {ue_interface})"
    if not quiet:
        print(report.to_text())  # noqa: T201
    return report
