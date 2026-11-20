"""
OAI F1 handover: the telnet command that triggers it, and the CU-CP and DU log lines that prove each step.

Trigger: ``ci trigger_f1_ho <CU UE ID>`` on the CU-CP telnet server (``--telnetsrv --telnetsrv.shrmod ci``); the UE is
named by the identifier the CU-CP gives it (CU UE ID, 1, 2... in the order of attachment), and the CU-CP picks the
target cell itself (the next cell among the connected DUs). The log lines below are those of OAI 2026.w42 to w46,
at the default log level of the R2Lab notebooks:

=========== ========= =================================================================================================
step        log of    line
=========== ========= =================================================================================================
decision    CU-CP     ``Handover triggered for UE <id>/RNTI <rnti> towards cell <cell>/assoc_id <a>/PCI <pci>``
preparation target DU ``Added new CFRA process for UE RNTI <new rnti> ... preamble(s): <preamble>``
command     CU-CP     ``HO acknowledged: Send reconfiguration for UE <id>/RNTI <rnti>``, then
                      ``UE <id>: update CU F1AP Context DU UE ID <a> => <b> and RNTI <rnti> => <new rnti>``
preamble    target DU ``TC-RNTI <new rnti>: initiating RA procedure for RAPID <preamble>``
response    target DU ``(rnti 0x<new rnti>) CFRA procedure succeeded!`` (logged when the DU sends the response, not
                      when the UE receives it); ``ignoring RAPID <preamble>`` afterwards: the UE sent the preamble
                      again, it did not decode the response
on target   target DU ``Received a MAC CE for C-RNTI with <new rnti>``: the UE, on the target cell, sent its new C-RNTI
                      in the Msg3 of a contention-based random access (it had no uplink grant it could use)
completion  CU-CP     ``(cellID <target cell>, UE ID <id> RNTI <rnti>) Received RRCReconfigurationComplete``
release     CU-CP     ``UE <id> Handover: trigger release on cell PCI <pci>`` (``handover for UE <id>/RNTI <rnti>
                      complete!`` just before)
=========== ========= =================================================================================================

After a failure: ``Reestablishment RNTI <r> req C-RNTI <c> physCellId <pci>`` (re-establishment), ``handover for UE
<id>/RNTI <rnti> failed, rollback`` (the CU-CP keeps the UE on the source cell), ``Create UE context: CU UE ID <id>``
(new attachment). OAI before 2026.w45 aborts the target DU ("Could not encode CellGroup") when the UE re-establishes
on it with SRB1 only; 2026.w46 still aborts it after such a failed handover on the R2Lab Benetel setup.

OAI 2026.w46 (``ue_context_setup_request``): the target DU creates the UE with the CellGroup built without the UE
capabilities (``SRS configured with 1 ports`` just before ``Added new CFRA process``), then sends the UE, in the
handover command, a copy updated with the capabilities of the UE (``SRS configured with 2 ports`` just after). After
the random access, it keeps scheduling the UE with the first one, until the UE acknowledges an RRC message sent by
this DU (``ack_reconfig``): when the two differ (SRS ports, uplink rank), the UE and the DU do not use the same DCI,
and the UE cannot send its RRCReconfigurationComplete.
"""

import re

from nador.handover.report import HandoverReport, Step, after_last, clean

__all__ = ["CONTAINERS", "analyze_oai_handover", "connected_ues", "trigger_command"]

# Container running the gNB, CU-CP or DU in each pod of the OAI charts (the others are sidecars).
CONTAINERS = ("gnb", "cu", "cucp", "cuup", "du")
# Lines read at the end of a DU log to tell which UEs it still serves (MAC statistics every 1.28 s).
STATS_TAIL_LINES = 300
# Lines around "Added new CFRA process" where the target DU logs the SRS ports of the two CellGroups of the UE.
SRS_LINES_BEFORE = 3
SRS_LINES_AFTER = 12
# CU-CP refusals of the trigger command.
REFUSALS = (
    "cannot trigger handover",
    "Ongoing handover for UE",
    "only one cell configured",
    "cannot find UE context for UE ID",
    "cannot get source cell for UE",
)


def trigger_command(ue_id: int, telnet_port: int) -> str:
    """
    Shell command, run in the CU-CP container, that sends ``ci trigger_f1_ho <ue_id>`` to the telnet server.

    The answer is read in full before closing: with OAI f80a883, closing earlier reset the connection and the
    telnet server aborted the CU-CP ("buffer overflow detected").
    """
    return (
        f"exec 3<>/dev/tcp/127.0.0.1/{telnet_port}; "
        f'printf "ci trigger_f1_ho {ue_id}\\n" >&3; '
        'while IFS= read -r -t 2 line <&3; do printf "%s\\n" "$line"; done; '
        "exec 3<&-; exec 3>&-"
    )


# CU-CP refusals that need an explanation: what the CU-CP checks, and what to do.
REFUSAL_HINTS = {
    "no DRB configured": (
        "the UE has no data radio bearer at the CU-CP: it registered but has no PDU session, and the target DU is "
        "prepared only for a UE with at least one DRB (TS 38.473, 8.3.1.2). Hand over a UE with a PDU session: "
        "nador.handover.oai.connected_ues() lists the UEs of the CU-CP and whether they have a DRB."
    ),
}


def connected_ues(cucp_log: str) -> list[dict]:
    """
    UE contexts of the CU-CP log still present, in creation order: CU UE ID, RNTI, cell, and whether a DRB exists.

    A UE without DRB registered but has no PDU session (no data): OAI refuses to hand it over. The CU UE ID of a UE that
    attached again is reused, so the last context of an ID is the one kept.
    """
    ues: dict[int, dict] = {}
    for line in cucp_log.splitlines():
        if m := re.search(r"\(cellID (\d+), UE ID (\d+) RNTI ([0-9a-f]+)\) Create UE context", line):
            ues[int(m.group(2))] = {"ue": int(m.group(2)), "rnti": m.group(3), "cell": int(m.group(1)), "drb": False}
        elif m := re.search(r"removed UE CU UE ID (\d+)/", line):
            ues.pop(int(m.group(1)), None)
        elif (m := re.search(r"UE (\d+): created new DRB", line)) and int(m.group(1)) in ues:
            ues[int(m.group(1))]["drb"] = True
        elif (m := re.search(r"UE (\d+): update CU F1AP Context .* RNTI [0-9a-f]+ => ([0-9a-f]+)", line)) and int(
            m.group(1)
        ) in ues:
            ues[int(m.group(1))]["rnti"] = m.group(2)
    return list(ues.values())


def _cucp(ue: int, lines: list[str]) -> dict:  # noqa: C901, PLR0912 (one branch per kind of log line)
    """CU-CP lines from the trigger on: decision, command, completion, release, and what followed a failure."""
    found: dict = {"rntis": [], "events": [], "complete": None, "complete_line": ""}
    triggered = rf"Handover triggered for UE {ue}/RNTI ([0-9a-f]+) towards cell (\d+)/assoc_id \d+/PCI (\d+)"
    updated = rf"UE {ue}: update CU F1AP Context DU UE ID \d+ => \d+ and RNTI ([0-9a-f]+) => ([0-9a-f]+)"
    completed = rf"\(cellID (\d+), UE ID {ue} RNTI ([0-9a-f]+)\) Received RRCReconfigurationComplete"
    for line in lines:
        if m := re.search(triggered, line):
            found["decision"] = line
            found["source_rnti"], found["target_cell"] = m.group(1), int(m.group(2))
            found["rntis"].append(m.group(1))
        elif any(refusal in line for refusal in REFUSALS):
            found.setdefault("refusal", line)
        elif re.search(rf"HO acknowledged: Send reconfiguration for UE {ue}/", line):
            found["command"] = line
        elif m := re.search(updated, line):
            found.setdefault("target_rnti", m.group(2))
            found["rntis"].append(m.group(2))
        elif m := re.search(completed, line):
            # only the first completion counts, and only if it comes before any re-establishment of the UE
            if found["complete"] is None and not found["events"]:
                found["complete"], found["complete_line"] = int(m.group(1)), line
        elif re.search(rf"UE {ue} Handover: trigger release on cell PCI \d+", line):
            found.setdefault("release", line)
        elif m := re.search(r"Reestablishment RNTI ([0-9a-f]+) req C-RNTI ([0-9a-f]+) physCellId (\d+)", line):
            if m.group(2) in found["rntis"]:
                found["rntis"].append(m.group(1))
                found["events"].append(
                    f"the UE requested an RRC re-establishment (RNTI {m.group(1)}, from C-RNTI {m.group(2)}, "
                    f"PCI {m.group(3)})",
                )
        elif re.search(rf"handover for UE {ue}/RNTI \S+ failed, rollback", line):
            found["events"].append("CU-CP: handover failed, the UE is kept on the source cell (rollback)")
        elif "without UE context, fallback to RRC setup" in line and found["events"]:
            found["events"].append("CU-CP: re-establishment without UE context, the UE attaches again (RRC setup)")
        elif re.search(rf"Create UE context: CU UE ID {ue} ", line) and "decision" in found:
            found["events"].append("the UE attached again (new UE context)")
    return found


def _srs_ports(new: str, lines: list[str]) -> tuple[int, int] | None:
    """SRS ports of the CellGroup without, then with, the UE capabilities, logged around the CFRA process of the UE."""
    prepared = [i for i, line in enumerate(lines) if f"Added new CFRA process for UE RNTI {new}" in line]
    if not prepared:
        return None
    i = prepared[-1]
    ports = [
        [int(m.group(1)) for line in part if (m := re.search(r"SRS configured with (\d+) ports", line))]
        for part in (lines[max(0, i - SRS_LINES_BEFORE) : i], lines[i + 1 : i + 1 + SRS_LINES_AFTER])
    ]
    return (ports[0][-1], ports[1][0]) if ports[0] and ports[1] else None


def _target_du(target: str, new: str | None, lines: list[str]) -> dict:  # noqa: C901 (one branch per kind of line)
    """Target DU lines: CFRA preparation of the UE, its preamble, the response, preamble sent again, abort."""
    found: dict = {"retransmissions": 0, "events": []}
    if new is None:  # no RNTI from the CU-CP: take the last CFRA process of the target DU
        cfra = [
            m.group(1) for line in lines if (m := re.search(r"Added new CFRA process for UE RNTI ([0-9a-f]+)", line))
        ]
        new = cfra[-1] if cfra else None
    if new is None:
        return found
    found["rnti"] = new
    found["c_rnti_ce"] = 0
    if ports := _srs_ports(new, lines):
        found["srs_ports"] = ports
    for line in after_last(lines, f"Added new CFRA process for UE RNTI {new}"):
        if m := re.search(rf"Added new CFRA process for UE RNTI {new} .*preamble\(s\):\s+(\d+)", line):
            found["preparation"], found["preamble"] = line, int(m.group(1))
        elif re.search(rf"TC-RNTI {new}: initiating RA procedure for RAPID (\d+)", line):
            found.setdefault("preamble_line", line)
        elif f"(rnti 0x{new}) CFRA procedure succeeded" in line:
            found.setdefault("response", line)
        elif "response" in found and re.search(rf"ignoring RAPID {found.get('preamble')}\b", line):
            found["retransmissions"] += 1
            found.setdefault("retransmission", line)
        elif f"Received a MAC CE for C-RNTI with {new}" in line:
            found["c_rnti_ce"] += 1
        elif re.search(rf"UE {new}: ulsch_rounds ", line):
            found["ul_stats"] = line
        elif "re-establishment with only one LCID" in line:
            found["events"].append(f"{target}: re-establishment with SRB1 only refused (OAI 2026.w45 and later)")
        elif "Could not encode CellGroup" in line or "Assertion" in line:
            found["events"].append(f"{target}: ABORTED: {clean(line.split(' F ', 1)[-1])}")
    return found


def _ue_location(ue: int, rntis: set[str], du_logs: dict[str, str]) -> dict[str, str]:
    """Last MAC statistics line of the UE on each DU (among its RNTIs), or 'not served'."""
    location = {}
    for du, text in du_logs.items():
        tail = text.splitlines()[-STATS_TAIL_LINES:]
        lines = [
            line for line in tail if re.search(rf"UE RNTI ([0-9a-f]+)(?: PCI \d+ CellID \d+)? CU-UE-ID {ue}\b", line)
        ]
        lines = [line for line in lines if re.search(r"UE RNTI ([0-9a-f]+)", line).group(1) in rntis] or lines
        location[du] = clean(lines[-1].split(" F ", 1)[-1]) if lines else "not served"
    return location


def analyze_oai_handover(
    ue_id: int, source: str, target: str, cucp_log: str, du_logs: dict[str, str]
) -> HandoverReport:
    """Analyze the logs of an OAI handover: CU-CP log (read from the trigger command on) and the logs of both DUs."""
    report = HandoverReport(implementation="OAI", ue=f"CU UE ID {ue_id}", source=source, target=target)
    cucp = _cucp(ue_id, after_last(cucp_log.splitlines(), f"trigger_f1_ho {ue_id}"))
    du = _target_du(target, cucp.get("target_rnti"), du_logs.get(target, "").splitlines())
    report.target_cell = cucp.get("target_cell")
    report.preamble = du.get("preamble")
    report.rntis = {
        k: v for k, v in (("source", cucp.get("source_rnti")), ("target", du.get("rnti"))) if v is not None
    } | {"chain": " ".join(cucp["rntis"])}
    report.ue_location = _ue_location(ue_id, set(cucp["rntis"]) | {du.get("rnti")}, du_logs)

    completed = cucp["complete"] is not None and cucp["complete"] == report.target_cell
    cell = f" towards cell {report.target_cell}" if report.target_cell is not None else ""
    again = f"then the preamble came again {du['retransmissions']} time(s)" if du["retransmissions"] else ""
    report.steps = [
        Step("decision", "decision" in cucp, f"RNTI {cucp.get('source_rnti', '?')}{cell}", cucp.get("decision", "")),
        Step(
            "preparation",
            "preparation" in du,
            f"new RNTI {du.get('rnti', '?')}, dedicated preamble {report.preamble}" if "preparation" in du else "",
            du.get("preparation", ""),
        ),
        Step("command", "command" in cucp, "", cucp.get("command", "")),
        Step("preamble", "preamble_line" in du, "", du.get("preamble_line", "")),
        Step("response", "response" in du, again, du.get("response", "")),
        Step("completion", completed, "", cucp["complete_line"] if completed else ""),
        Step("release", "release" in cucp, "", cucp.get("release", "")),
    ]
    for step in report.steps:
        step.evidence = clean(step.evidence)
    report.aftermath = cucp["events"] + du["events"]
    if "ul_stats" in du:
        report.aftermath.append(f"{target}: uplink of RNTI {du['rnti']}: {clean(du['ul_stats'])}")
    report.success = completed

    if report.success:
        report.diagnosis = "The UE completed the handover on the target cell."
    elif "decision" not in cucp:
        refusal = f" It answered: {clean(cucp['refusal'])}" if "refusal" in cucp else ""
        hints = [hint for key, hint in REFUSAL_HINTS.items() if key in cucp.get("refusal", "")]
        report.diagnosis = (
            f"The CU-CP did not trigger the handover: check the CU UE ID and the telnet answer.{refusal}"
            + (f" Meaning: {hints[0]}" if hints else "")
        )
    elif "preparation" not in du:
        report.diagnosis = f"{target} did not prepare the UE (no CFRA process): UE context setup failed on the target."
    elif "preamble_line" not in du:
        report.diagnosis = (
            f"{target} never received the dedicated preamble {report.preamble}: the UE did not execute the "
            "handover command (not received from the source cell), or its preamble did not reach the target RU."
        )
    elif du.get("c_rnti_ce"):
        report.diagnosis = (
            f"The UE reached the target cell: it sent its new C-RNTI {du['rnti']} to {target} in "
            f"{du['c_rnti_ce']} contention-based random access(es), but {target} did not decode its uplink "
            "afterwards, and its RRCReconfigurationComplete never reached the CU-CP."
        )
        before, after = du.get("srs_ports", (None, None))
        if before is not None and before != after:
            report.diagnosis += (
                f" {target} prepared the UE with a CellGroup without its capabilities (SRS on {before} port(s)) and "
                f"sent it a CellGroup with its capabilities (SRS on {after} port(s)): OAI keeps scheduling the UE with "
                "the first one after the random access, so the UE and the DU do not use the same uplink configuration."
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
