"""
OCUDU F1 handover: the console command that triggers it, and the CU-CP and DU log lines that prove each step.

Trigger: ``ho <serving PCI> <C-RNTI> <target PCI>`` on the console (standard input) of the CU-CP, which prints
``Handover triggered for UE with pci=<s> rnti=<rnti> to pci=<t>.``; the UE is named by its C-RNTI in the serving cell.
The console is the only way to send it (no remote control command): the CU-CP container must be started with
``stdin: true`` (nador ``ocudu-cu_cp`` chart), and the command is written to it with ``kubectl attach -i``.

The CU-CP numbers its DUs (du=0, du=1... in the order of their F1 setup) and its UEs (ue=0, 1...): the handover creates
a second UE index for the UE on the target DU. At the default ``info`` log level, the CU-CP logs every F1AP message
(``Tx PDU du=<du> ue=<ue> ...: <message>``) and the RRC messages of each UE (``ue=<ue> c-rnti=<rnti>: DCCH UL
<message>``); the DU logs its procedures (``ue=<ue> rnti=<rnti> proc="UE Create": ...``) and the PRACH it detects
(``Processed slot events pci=<pci>: ... prach(ra-rnti=<r> preamble=<p> tc-rnti=<rnti>)``):

=========== ========= =================================================================================================
step        log of    line
=========== ========= =================================================================================================
decision    CU-CP     ``Handover triggered for UE with pci=<s> rnti=<rnti> to pci=<t>.``, then
                      ``ue=<ue>: Trigger intra-CU (inter-DU) handover from source_du=<s> to target_du=<t>``
preparation CU-CP     ``Tx PDU du=<target> ue=<target ue> ...: UEContextSetupRequest``, then
                      ``Rx PDU du=<target> ...: UEContextSetupResponse`` (``UEContextSetupFailure``: refused);
            target DU ``ue=<ue> rnti=<new rnti> proc="UE Create": Procedure finished successfully.``
command     CU-CP     ``Tx PDU du=<source> ue=<ue> ...: UEContextModificationRequest`` (it carries the
                      RRCReconfiguration)
preamble    target DU ``prach(ra-rnti=<r> preamble=<p> tc-rnti=<new rnti>)``: with contention-free random access
                      (``prach: cfra_enabled: true`` in the DU cell), the dedicated preamble comes with the new C-RNTI;
                      with contention-based access, the UE gets a temporary RNTI and gives its C-RNTI in Msg3, which
                      the DU logs only at the ``debug`` level
response    -         not logged at the ``info`` level
completion  CU-CP     ``ue=<target ue> c-rnti=<new rnti>: DCCH UL rrcReconfigurationComplete``, or
                      ``Rx PDU du=<target> ue=<target ue> ...: ULRRCMessageTransfer``
release     CU-CP     ``Tx PDU du=<source> ue=<ue> ...: UEContextReleaseCommand``
=========== ========= =================================================================================================

After a failure: ``"Intra CU Handover Routine" failed ...`` (the CU-CP gave up: target DU refused the UE, RRC
reconfiguration not sent), ``UEContextReleaseCommand`` to the target DU for the target UE (handover cancelled or
timed out), ``CCCH UL rrcReestablishmentRequest`` (re-establishment), ``CCCH UL rrcSetupRequest`` (new attachment).

These log lines come from the source of the srsRAN Project gNB (``main``, February 2026), from which OCUDU derives:
check them against the logs of the OCUDU release used, and adapt the patterns below if a step is reported missing
while its line is in the logs.
"""

import re

from nador.handover.report import HandoverReport, Step, after_last, clean

__all__ = ["CONTAINERS", "analyze_ocudu_handover", "connected_ues", "format_rnti", "trigger_command"]

# Container running the gNB, CU, CU-CP or DU in each pod of the OCUDU charts (the others are sidecars).
CONTAINERS = ("gnb", "cu", "cucp", "cuup", "du")
F1AP = re.compile(r"(Tx|Rx) PDU du=(\d+)(?: tid=\d+)?(?: ue=(\d+))?(?: cu_ue=\d+)?(?: du_ue=\d+)?: (\w+)")
# Log lines read before the answer of the console (see _from_trigger).
TRIGGER_LOOKBACK_LINES = 20
REFUSALS = (
    "Could not trigger handover",
    "Ignoring Handover Request",
    "Couldn't retrieve CGI",
    "Invalid handover command structure",
    "Invalid serving PCI",
    "Invalid UE RNTI",
    "Invalid target PCI",
)


def format_rnti(rnti: int | str) -> str:
    """C-RNTI as OCUDU prints it: '0x4601' (from 0x4601, '4601' or '0x4601')."""
    return f"{int(rnti, 16) if isinstance(rnti, str) else rnti:#x}"


def trigger_command(serving_pci: int, rnti: int | str, target_pci: int) -> str:
    """Console command of the OCUDU CU-CP that hands the UE of C-RNTI rnti over from serving_pci to target_pci."""
    return f"ho {serving_pci} {format_rnti(rnti)} {target_pci}"


def connected_ues(cucp_log: str) -> list[dict]:
    """
    UEs of the CU-CP log, in attachment order: UE index on the CU-CP, DU index and C-RNTI in its cell.

    The DU index comes from the F1AP InitialULRRCMessageTransfer of the UE (du=0 for the first DU set up), the C-RNTI
    from its RRC log prefix (``ue=<ue> c-rnti=<rnti>: CCCH UL rrcSetupRequest``). The C-RNTI is the one to give to
    run_handover for a UE served by the source DU.
    """
    ues: dict[int, dict] = {}
    for line in cucp_log.splitlines():
        pdu = _f1ap(line)
        if pdu and pdu[0] == "Rx" and pdu[3] == "InitialULRRCMessageTransfer" and pdu[2] is not None:
            ues[pdu[2]] = {"ue": pdu[2], "du": pdu[1], "c_rnti": None}
        elif m := re.search(r"ue=(\d+) c-rnti=(0x[0-9a-f]+): (?:CCCH|DCCH) UL rrcSetup", line):
            ues.setdefault(int(m.group(1)), {"ue": int(m.group(1)), "du": None, "c_rnti": None})["c_rnti"] = m.group(2)
    return list(ues.values())


def _f1ap(line: str) -> tuple[str, int, int | None, str] | None:
    """(Tx or Rx, DU index, UE index or None, message) of an F1AP log line of the CU-CP."""
    if m := F1AP.search(line):
        return m.group(1), int(m.group(2)), int(m.group(3)) if m.group(3) else None, m.group(4)
    return None


def _cucp(rnti: str, lines: list[str]) -> dict:  # noqa: C901, PLR0912 (one branch per kind of log line)
    """CU-CP lines from the trigger on: decision, preparation, command, completion, release, failures."""
    found: dict = {"events": []}
    triggered = rf"Handover triggered for UE with pci=(\d+) rnti={rnti} to pci=(\d+)"
    inter_du = r"ue=(\d+): Trigger intra-CU \(inter-DU\) handover from source_du=(\d+) to target_du=(\d+)"
    for line in lines:
        pdu = _f1ap(line)
        if m := re.search(triggered, line):
            found["trigger"], found["target_pci"] = line, int(m.group(2))
        elif m := re.search(inter_du, line):
            found["decision"] = line
            found["ue"], found["source_du"], found["target_du"] = (int(g) for g in m.groups())
        elif "Trigger intra-CU (intra-DU) handover" in line:
            found["events"].append("the CU-CP started an intra-DU handover: the target PCI is a cell of the source DU")
        elif any(refusal in line for refusal in REFUSALS):
            found.setdefault("refusal", line)
        elif '"Intra CU Handover Routine"' in line and ("failed" in line or "Failed" in line):
            found["events"].append(f"CU-CP: {clean(line.split('ue=', 1)[-1])}")
        elif pdu and "decision" in found:
            direction, du, ue, message = pdu
            source, target = du == found["source_du"], du == found["target_du"]
            if target and direction == "Tx" and message == "UEContextSetupRequest":
                found["target_ue"], found["setup_request"] = ue, line
            elif target and direction == "Rx" and message in ("UEContextSetupResponse", "UEContextSetupFailure"):
                found.setdefault("setup_answer", line)
            elif source and ue == found["ue"] and direction == "Tx" and message == "UEContextModificationRequest":
                found.setdefault("command", line)
            elif target and ue == found.get("target_ue") and direction == "Rx" and message == "ULRRCMessageTransfer":
                found.setdefault("ul_rrc", line)
            elif source and ue == found["ue"] and direction == "Tx" and message == "UEContextReleaseCommand":
                found.setdefault("release", line)
            elif target and ue == found.get("target_ue") and direction == "Tx" and message == "UEContextReleaseCommand":
                found["events"].append(f"CU-CP released the UE on the target DU (du={du}): handover cancelled")
        elif "DCCH UL rrcReconfigurationComplete" in line and f"ue={found.get('target_ue')} " in line:
            found.setdefault("complete", line)
        elif "CCCH UL rrcReestablishmentRequest" in line and "decision" in found:
            found["events"].append("a UE requested an RRC re-establishment (rrcReestablishmentRequest)")
        elif "CCCH UL rrcSetupRequest" in line and "decision" in found:
            found["events"].append("a UE attached (rrcSetupRequest): the UE may have lost the link and attached again")
    return found


def _target_du(lines: list[str], target_pci: int | None) -> dict:
    """Target DU lines from the UE context setup on: new C-RNTI of the UE, PRACH detected on the target cell."""
    found: dict = {"prach": []}
    lines = after_last(lines, "UEContextSetupRequest")
    for line in lines:
        if m := re.search(r'ue=\d+ rnti=(0x[0-9a-f]+) proc="UE Create": Procedure finished successfully', line):
            found.setdefault("rnti", m.group(1))
            found.setdefault("created", line)
    pci = rf"pci={target_pci}\b" if target_pci is not None else r"pci=\d+"
    for line in lines:
        if re.search(rf"Processed slot events {pci}", line):
            found["prach"] += [(line, int(p), r) for p, r in re.findall(r"preamble=(\d+) tc-rnti=(0x[0-9a-f]+)", line)]
    return found


def _from_trigger(lines: list[str], needle: str) -> list[str]:
    """
    Lines from the last trigger of the console on, with the lines just before it.

    The console prints its answer after the CU-CP has started the handover: the first log lines of the handover may
    come before it.
    """
    for index in range(len(lines) - 1, -1, -1):
        if needle in lines[index]:
            return lines[max(0, index - TRIGGER_LOOKBACK_LINES) :]
    return []


def analyze_ocudu_handover(
    rnti: int | str,
    source: str,
    target: str,
    cucp_log: str,
    du_logs: dict[str, str],
) -> HandoverReport:
    """Analyze the logs of an OCUDU handover: CU-CP log (read from the trigger command on) and the logs of both DUs."""
    rnti = format_rnti(rnti)
    report = HandoverReport(implementation="OCUDU", ue=f"C-RNTI {rnti}", source=source, target=target)
    cucp = _cucp(rnti, _from_trigger(cucp_log.splitlines(), f"rnti={rnti} to pci="))
    du = _target_du(du_logs.get(target, "").splitlines(), cucp.get("target_pci"))
    report.target_cell = cucp.get("target_pci")
    new = du.get("rnti")
    report.rntis = {"source": rnti} | ({"target": new} if new else {})

    cfra = [(line, preamble) for line, preamble, tc_rnti in du["prach"] if new is not None and tc_rnti == new]
    if cfra:
        report.preamble = cfra[0][1]
        preamble = Step("preamble", True, f"dedicated preamble {report.preamble} (contention-free)", cfra[0][0])
    elif du["prach"]:
        preamble = Step(
            "preamble",
            None,
            f"{len(du['prach'])} PRACH on the target cell, none with the new C-RNTI: contention-based access, the UE "
            "gives its C-RNTI in Msg3 (logged at the debug level only)",
            du["prach"][0][0],
        )
    else:
        preamble = Step("preamble", False, "no PRACH on the target cell after the preparation")
    again = len(cfra) - 1
    setup_ok = "UEContextSetupResponse" in cucp.get("setup_answer", "")
    completed = "complete" in cucp or "ul_rrc" in cucp
    du_index = f"du={cucp['source_du']} -> du={cucp['target_du']}, " if "decision" in cucp else ""
    report.steps = [
        Step(
            "decision",
            "decision" in cucp,
            f"{du_index}UE ue={cucp['ue']} on the CU-CP" if "decision" in cucp else "",
            cucp.get("decision", cucp.get("trigger", "")),
        ),
        Step(
            "preparation",
            setup_ok,
            (f"target UE ue={cucp.get('target_ue')}" + (f", new C-RNTI {new}" if new else "")) if setup_ok else "",
            du.get("created", cucp.get("setup_answer", "")),
        ),
        Step("command", "command" in cucp, "UE context modification of the source DU", cucp.get("command", "")),
        preamble,
        Step(
            "response",
            None,
            f"not logged at the info level; the preamble came again {again} time(s)"
            if again > 0
            else "not logged at the info level",
        ),
        Step("completion", completed, "", cucp.get("complete", cucp.get("ul_rrc", ""))),
        Step("release", "release" in cucp, "", cucp.get("release", "")),
    ]
    for step in report.steps:
        step.evidence = clean(step.evidence)
    report.aftermath = cucp["events"]
    report.success = completed
    report.notes.append(
        "OCUDU log lines taken from the srsRAN Project source: if a step is missing while its line is in the logs, "
        "adapt the patterns of nador.handover.ocudu.",
    )

    if report.success:
        report.diagnosis = "The UE completed the handover on the target cell."
    elif "decision" not in cucp:
        refusal = f" It answered: {clean(cucp['refusal'])}" if "refusal" in cucp else ""
        trigger = "" if "trigger" in cucp else " (the console command did not reach the CU-CP)"
        report.diagnosis = (
            f"The CU-CP did not start an inter-DU handover{trigger}: check the serving PCI, the C-RNTI of the UE in "
            f"the serving cell, and the target PCI.{refusal}"
        )
    elif not setup_ok:
        report.diagnosis = (
            f"{target} did not accept the UE (no UEContextSetupResponse): UE context setup failed on the target."
        )
    elif "command" not in cucp:
        report.diagnosis = "The CU-CP did not send the handover command to the source DU (RRCReconfiguration)."
    elif preamble.ok is False:
        report.diagnosis = (
            f"No random access on {target} after the handover command: the UE did not receive or execute it, or its "
            "preamble did not reach the target RU."
        )
    elif again > 0:
        report.diagnosis = (
            "The UE did not decode the random access response of the target cell: it sent its dedicated "
            "preamble again (downlink of the target cell at the UE)."
        )
    else:
        report.diagnosis = (
            "The UE was prepared and commanded, but its RRCReconfigurationComplete never reached the CU-CP through "
            "the target DU (random access not completed, or uplink of the target cell)."
        )
    return report
