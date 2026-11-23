"""
OCUDU F1 handover: the remote command that triggers it, and the CU-CP and DU log lines that prove each step.

Trigger: remote command ``{"cmd": "trigger_handover", "serving_pci": <s>, "rnti": <C-RNTI>, "target_pci": <t>,
"plmn": "<target PLMN>", "tac": <target TAC>}`` (integers, PLMN as a string of 5 or 6 digits) sent to the remote
control server of the CU-CP (WebSocket, ``remote_control: {enabled: true, bind_addr: 0.0.0.0, port: <port>}`` in the
CU-CP configuration); the UE is named by its C-RNTI in the serving cell. The answer is a JSON object, with ``error``
when the command is refused. The CU-CP runs the trigger on its executor and waits at most 5 s for it
(``dispatch_bounded``, lib/cu_cp/bounded_executor_dispatch.h): the remote control server has its own thread
(``ws_server``). The console command ``ho <serving PCI> <C-RNTI> <target PCI> <PLMN> <TAC>`` reaches the same trigger
from the command line executor (``workers.get_cmd_line_executor()``), and timed out on the R2Lab CU-CP
(``Dispatch trigger_handover timed out after 5s. The command was cancelled and does not run``): it is not used.

The CU-CP numbers its UEs (ue=0, 1...): the handover creates a second UE index for the UE on the target DU. Its F1AP
lines name the DU by its gNB-DU ID (``gnb_du_id`` of the DU configuration: ``Added TNL connection to DU 0`` then ``Rx
PDU du=1 tid=0: F1SetupRequest`` for gnb_du_id 1), its mobility lines by its DU index (0, 1... in the order of the
connections). At the default ``info`` log level, the CU-CP logs every F1AP message
(``Tx PDU du=<gNB-DU ID> ue=<ue> ...: <message>``) and the RRC messages of each UE (``ue=<ue> c-rnti=<rnti>: DCCH UL
<message>``); the DU logs its procedures (``ue=<ue> rnti=<rnti> proc="UE Create": ...``) and the PRACH it detects
(``Processed slot events pci=<pci>: ... prach(ra-rnti=<r> preamble=<p> tc-rnti=<rnti>)``):

=========== ========= =================================================================================================
step        log of    line
=========== ========= =================================================================================================
decision    CU-CP     ``ue=<ue>: Trigger intra-CU (inter-DU) handover from source_du=<s> to target_du=<t>``
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

The lines of a successful handover (decision, preparation, command, PRACH of the target DU, completion, release) and
of the UE attachments were checked against R2Lab logs (OCUDU image 920f88c7, 2026-11-20), the remote command against
the OCUDU source (``main``, November 2026); the failure lines come from the source of the srsRAN Project gNB
(``main``, February 2026), from which OCUDU derives: adapt their patterns if a failure is not recognised.
"""

import re

from nador.handover.report import HandoverReport, Step, after_last, clean

__all__ = ["CONTAINERS", "analyze_ocudu_handover", "connected_ues", "format_rnti", "trigger_request"]

# Container running the gNB, CU, CU-CP or DU in each pod of the OCUDU charts (the others are sidecars).
CONTAINERS = ("gnb", "cu", "cucp", "cuup", "du")
F1AP = re.compile(r"(Tx|Rx) PDU du=(\d+)(?: tid=\d+)?(?: ue=(\d+))?(?: cu_ue=\d+)?(?: du_ue=\d+)?: (\w+)")
REFUSALS = (
    "Could not trigger handover",
    "Ignoring Handover Request",
    "Couldn't retrieve CGI",
    "Dispatch trigger_handover",
)


def format_rnti(rnti: int | str) -> str:
    """C-RNTI as OCUDU prints it: '0x4601' (from 0x4601, '4601' or '0x4601')."""
    return f"{int(rnti, 16) if isinstance(rnti, str) else rnti:#x}"


def trigger_request(serving_pci: int, rnti: int | str, target_pci: int, target_plmn: str, target_tac: int) -> dict:
    """
    Remote command of the OCUDU CU-CP that hands the UE of C-RNTI rnti over from serving_pci to target_pci.

    target_plmn (5 or 6 digits, 00101) and target_tac are those of the target cell.
    """
    return {
        "cmd": "trigger_handover",
        "serving_pci": serving_pci,
        "rnti": int(format_rnti(rnti), 16),
        "target_pci": target_pci,
        "plmn": target_plmn,
        "tac": target_tac,
    }


def connected_ues(cucp_log: str) -> list[dict]:
    """
    UEs attached to the CU-CP at the end of its log, in attachment order: UE index, gNB-DU ID and C-RNTI in its cell.

    A UE is added by its first RRC line (``ue=<ue> c-rnti=<rnti>: CCCH UL rrcSetupRequest``), its DU being the one of
    the F1AP InitialULRRCMessageTransfer just before (``Rx PDU du=<du> tid=<t> du_ue=<i>: ...``, without UE index), or
    by the UEContextSetupRequest of a handover to its DU. It is removed by ``ue=<ue>: "UE Removal Routine" finished``,
    which follows every release: the CU-CP releases an inactive UE after cu_cp inactivity_timer (120 s by default), and
    the UE attaches again with a new UE index and C-RNTI when it has data to send. The C-RNTI is the one to give to
    run_handover for a UE served by the source DU.
    """
    ues: dict[int, dict] = {}
    initial_du = None
    for line in cucp_log.splitlines():
        pdu = _f1ap(line)
        if pdu and pdu[3] == "InitialULRRCMessageTransfer":
            initial_du = pdu[1]
        elif m := re.search(r"ue=(\d+) c-rnti=(0x[0-9a-f]+): CCCH UL rrcSetupRequest", line):
            ues[int(m.group(1))] = {"ue": int(m.group(1)), "du": initial_du, "c_rnti": m.group(2)}
        elif (m := re.search(r"ue=(\d+) c-rnti=(0x[0-9a-f]+): ", line)) and int(m.group(1)) in ues:
            ues[int(m.group(1))]["c_rnti"] = m.group(2)
        elif pdu and pdu[0] == "Tx" and pdu[3] == "UEContextSetupRequest" and pdu[2] is not None:
            ues.setdefault(pdu[2], {"ue": pdu[2], "du": pdu[1], "c_rnti": None})
        elif m := re.search(r'ue=(\d+): "UE Removal Routine" finished', line):
            ues.pop(int(m.group(1)), None)
    return list(ues.values())


def _f1ap(line: str) -> tuple[str, int, int | None, str] | None:
    """(Tx or Rx, gNB-DU ID, UE index or None, message) of an F1AP log line of the CU-CP."""
    if m := F1AP.search(line):
        return m.group(1), int(m.group(2)), int(m.group(3)) if m.group(3) else None, m.group(4)
    return None


def _cucp(lines: list[str], answer: str) -> dict:  # noqa: C901, PLR0912 (one branch per kind of log line)
    """CU-CP lines from the trigger on and answer to the remote command: decision, preparation... failures."""
    found: dict = {"events": []}
    if '"error"' in answer:
        found["refusal"] = answer
    inter_du = r"ue=(\d+): Trigger intra-CU \(inter-DU\) handover from source_du=(\d+) to target_du=(\d+)"
    for line in lines:
        pdu = _f1ap(line)
        if m := re.search(inter_du, line):
            found["decision"] = line
            found["ue"], found["source_du"], found["target_du"] = (int(g) for g in m.groups())
        elif "Trigger intra-CU (intra-DU) handover" in line:
            found["events"].append("the CU-CP started an intra-DU handover: the target PCI is a cell of the source DU")
        elif any(refusal in line for refusal in REFUSALS):
            found.setdefault("refusal", line)
        elif '"Intra CU Handover Routine"' in line and ("failed" in line or "Failed" in line):
            found["events"].append(f"CU-CP: {clean(line.split('ue=', 1)[-1])}")
        elif pdu and "decision" in found:
            # F1AP lines give the gNB-DU ID (du=), the decision line the DU index of the CU-CP: match on UE indexes
            direction, du, ue, message = pdu
            if direction == "Tx" and message == "UEContextSetupRequest" and ue != found["ue"]:
                found.setdefault("target_ue", ue)
            source, target = ue == found["ue"], ue == found.get("target_ue")
            if target and direction == "Tx" and message == "UEContextSetupRequest":
                found["setup_request"] = line
            elif target and direction == "Rx" and message in ("UEContextSetupResponse", "UEContextSetupFailure"):
                found.setdefault("setup_answer", line)
            elif source and direction == "Tx" and message == "UEContextModificationRequest":
                found.setdefault("command", line)
            elif target and direction == "Rx" and message == "ULRRCMessageTransfer":
                found.setdefault("ul_rrc", line)
            elif source and direction == "Tx" and message == "UEContextReleaseCommand":
                found.setdefault("release", line)
            elif target and direction == "Tx" and message == "UEContextReleaseCommand":
                found["events"].append(f"CU-CP released the UE on the target DU (du={du}): handover cancelled")
        elif "DCCH UL rrcReconfigurationComplete" in line and f"ue={found.get('target_ue')} " in line:
            found.setdefault("complete", line)
        elif "CCCH UL rrcReestablishmentRequest" in line and "decision" in found:
            found["events"].append("a UE requested an RRC re-establishment (rrcReestablishmentRequest)")
        elif "CCCH UL rrcSetupRequest" in line and "decision" in found:
            found["events"].append(
                "a UE attached (rrcSetupRequest): another UE, or this one after losing the link (see the ping)"
            )
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


def _preamble_step(prach: list, cfra: list, target: str, completed: bool) -> Step:
    """Random access step: dedicated preamble with the new C-RNTI, other PRACH, or none in the target DU log."""
    if cfra:
        return Step("preamble", True, f"dedicated preamble {cfra[0][1]} (contention-free)", cfra[0][0])
    if prach:
        return Step(
            "preamble",
            None,
            f"{len(prach)} PRACH on the target cell, none with the new C-RNTI: contention-based access, the UE "
            "gives its C-RNTI in Msg3 (logged at the debug level only)",
            prach[0][0],
        )
    if completed:
        # the UE did its random access on the target cell: the target DU log does not cover the handover (rotated
        # by kubelet, or PRACH not logged at the configured level)
        return Step("preamble", None, f"no PRACH line in the log of {target}, which does not cover the handover")
    return Step("preamble", False, "no PRACH on the target cell after the preparation")


def analyze_ocudu_handover(
    rnti: int | str,
    source: str,
    target: str,
    cucp_log: str,
    du_logs: dict[str, str],
    *,
    target_pci: int | None = None,
    answer: str = "",
) -> HandoverReport:
    """
    Analyze the logs of an OCUDU handover, read from the trigger on: CU-CP log and logs of both DUs.

    target_pci is the PCI of the target cell, answer the JSON answer of the CU-CP to the remote command.
    """
    rnti = format_rnti(rnti)
    report = HandoverReport(implementation="OCUDU", ue=f"C-RNTI {rnti}", source=source, target=target)
    cucp = _cucp(cucp_log.splitlines(), answer)
    du = _target_du(du_logs.get(target, "").splitlines(), target_pci)
    report.target_cell = target_pci
    new = du.get("rnti")
    report.rntis = {"source": rnti} | ({"target": new} if new else {})

    cfra = [(line, preamble) for line, preamble, tc_rnti in du["prach"] if new is not None and tc_rnti == new]
    if cfra:
        report.preamble = cfra[0][1]
    completed = "complete" in cucp or "ul_rrc" in cucp
    preamble = _preamble_step(du["prach"], cfra, target, completed)
    again = len(cfra) - 1
    setup_ok = "UEContextSetupResponse" in cucp.get("setup_answer", "")
    du_index = f"DU index {cucp['source_du']} -> {cucp['target_du']}, " if "decision" in cucp else ""
    report.steps = [
        Step(
            "decision",
            "decision" in cucp,
            f"{du_index}UE ue={cucp['ue']} on the CU-CP" if "decision" in cucp else "",
            cucp.get("decision", ""),
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

    if report.success:
        report.diagnosis = "The UE completed the handover on the target cell."
    elif "decision" not in cucp:
        refusal = f" It answered: {clean(cucp['refusal'])}" if "refusal" in cucp else ""
        report.diagnosis = (
            "The CU-CP did not start an inter-DU handover: check the serving PCI, the C-RNTI of the UE in the serving "
            f"cell, and the target PCI.{refusal}"
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
