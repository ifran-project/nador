"""
Analysis of an OAI F1 handover: how it is triggered, and which CU-CP and DU log lines prove each step.

The steps of the handover and their numbering are explained in the package documentation (:mod:`nador.handover`).
This module only says how OAI implements them and how to recognise them in the OAI logs.

Triggering the handover
=======================

The OAI CU-CP has a telnet server (options ``--telnetsrv --telnetsrv.shrmod ci``). The command is::

    ci trigger_f1_ho <CU UE ID>

- The UE is named by its **CU UE ID**: the number the CU-CP gives each UE when it attaches (1, 2, ...), shown in
  the CU-CP log (``Create UE context: CU UE ID 2``) and by :func:`connected_ues`.
- The target is **not** given: the CU-CP picks the next cell among the DUs connected to it (with two DUs, the other
  one).
- The CU-CP answers ``RRC F1 handover triggered for UE <id>`` when it starts the handover, or a refusal (unknown UE,
  handover already running, single cell...): see ``REFUSALS``. An accepted command only means that the handover
  started; only the logs tell whether it succeeded.

Reading the OAI logs
====================

- The CU-CP log has no timestamps; the DU log lines start with a time in seconds (``2098.765438``).
- RNTIs are printed in hexadecimal without ``0x`` (``RNTI 7e3e``), except in a few MAC lines (``rnti 0x9003``).
- During a handover the UE has two RNTIs: its C-RNTI in the source cell, and the new one allocated by the target DU
  (``RNTI 7e3e`` then ``RNTI 9003``). The CU UE ID does not change.

Each step and the line that proves it (OAI 2026.w42 to w46, default log level of the R2Lab notebooks):

=========== ========= ================================================================================================
step        log of    line that proves it
=========== ========= ================================================================================================
decision    CU-CP     ``Handover triggered for UE <id>/RNTI <rnti> towards cell <cell>/assoc_id <a>/PCI <pci>``
preparation target DU ``Added new CFRA process for UE RNTI <new rnti> ... preamble(s): <preamble>``: the target DU
                      created the UE, with its new C-RNTI and a dedicated preamble
command     CU-CP     ``HO acknowledged: Send reconfiguration for UE <id>/RNTI <rnti>``, then
                      ``UE <id>: update CU F1AP Context DU UE ID <a> => <b> and RNTI <rnti> => <new rnti>``
preamble    target DU ``TC-RNTI <new rnti>: initiating RA procedure for RAPID <preamble>``: the dedicated preamble
response    target DU ``(rnti 0x<new rnti>) CFRA procedure succeeded!``
completion  CU-CP     ``(cellID <target cell>, UE ID <id> RNTI <rnti>) Received RRCReconfigurationComplete``
release     CU-CP     ``UE <id> Handover: trigger release on cell PCI <pci>`` (after ``handover for UE <id>/RNTI
                      <rnti> complete!``)
=========== ========= ================================================================================================

Two lines need care:

- ``CFRA procedure succeeded!`` is written when the target DU **sends** the random access response, not when the UE
  receives it. It does not prove that the UE decoded the response, nor that its uplink works.
- What the target DU logs next tells what went wrong when the completion is missing:

  - ``ignoring RAPID <preamble>``: the UE sent its dedicated preamble again, so it did not decode the response
    (problem on the downlink of the target cell, at the UE);
  - ``Received a MAC CE for C-RNTI with <new rnti>``: the UE **is** on the target cell. It had no uplink grant it
    could use, so it made a contention-based random access and gave its new C-RNTI in Msg3. The target DU does not
    decode its other uplink transmissions (the uplink statistics of the UE, ``UE <rnti>: ulsch_rounds ...``, show
    the errors and DTX).

After a failure, the CU-CP logs ``Reestablishment RNTI <r> req C-RNTI <c> physCellId <pci>`` (the UE asks for an RRC
re-establishment; the C-RNTI tells in which cell it lost the link), ``handover for UE <id>/RNTI <rnti> failed,
rollback`` (the CU-CP keeps the UE on the source cell) or ``Create UE context: CU UE ID <id>`` (new attachment).

Known OAI issues
================

- **Target DU and UE with different configurations** (OAI up to 2026.w47 at least). The target DU creates the UE
  with a CellGroupConfig built without the UE capabilities (``SRS configured with 1 ports`` just before ``Added new
  CFRA process``), but the handover command gives the UE a copy updated with its capabilities (``SRS configured with
  2 ports`` just after). The target DU keeps scheduling the UE with the first one until the handover completes; when
  they differ (SRS ports, uplink rank, hence the size of the DCI), the UE cannot send its RRCReconfigurationComplete:
  the second case above. This happens with ``pusch_AntennaPorts`` > 1, periodic SRS and UEs announcing 2 SRS ports
  (Quectel). Fixed by commit f274937 (duranta-project, Robert Schmidt), which applies the configuration at once on
  the target DU, and applied in the R2Lab OAI images. The analysis reports this case when the SRS ports differ.
- **Target DU abort** (``Could not encode CellGroup``) when the UE re-establishes on it after a failed handover: seen
  with OAI 2026.w46 on the R2Lab Benetel setup; earlier versions also abort when the UE re-establishes with SRB1 only.
"""

import re

from nador.handover.report import HandoverReport, Step, after_last, clean

__all__ = ["CONTAINERS", "analyze_oai_handover", "connected_ues"]

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
        pattern = re.compile(rf"UE RNTI ([0-9a-f]+)(?: PCI \d+ CellID \d+)? CU-UE-ID {ue}\b")
        matches = [(line, match.group(1)) for line in tail if (match := pattern.search(line))]
        lines = [line for line, rnti in matches if rnti in rntis] or [line for line, _ in matches]
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
    rntis = {rnti for rnti in [*cucp["rntis"], du.get("rnti")] if rnti is not None}
    report.ue_location = _ue_location(ue_id, rntis, du_logs)

    report.success = cucp["complete"] is not None and cucp["complete"] == report.target_cell
    report.steps = _steps(cucp, du, report)
    for step in report.steps:
        step.evidence = clean(step.evidence)
    report.aftermath = cucp["events"] + du["events"]
    if "ul_stats" in du:
        report.aftermath.append(f"{target}: uplink of RNTI {du['rnti']}: {clean(du['ul_stats'])}")
    report.diagnosis = _diagnosis(report, cucp, du, target)
    return report


def _steps(cucp: dict, du: dict, report: HandoverReport) -> list[Step]:
    """The steps of the procedure, as the CU-CP and target DU logs show them."""
    cell = f" towards cell {report.target_cell}" if report.target_cell is not None else ""
    prepared = f"new RNTI {du.get('rnti', '?')}, dedicated preamble {report.preamble}" if "preparation" in du else ""
    again = f"then the preamble came again {du['retransmissions']} time(s)" if du["retransmissions"] else ""
    completed = report.success
    return [
        Step("decision", "decision" in cucp, f"RNTI {cucp.get('source_rnti', '?')}{cell}", cucp.get("decision", "")),
        Step("preparation", "preparation" in du, prepared, du.get("preparation", "")),
        Step("command", "command" in cucp, "", cucp.get("command", "")),
        Step("preamble", "preamble_line" in du, "", du.get("preamble_line", "")),
        Step("response", "response" in du, again, du.get("response", "")),
        Step("completion", completed, "", cucp["complete_line"] if completed else ""),
        Step("release", "release" in cucp, "", cucp.get("release", "")),
    ]


def _diagnosis(report: HandoverReport, cucp: dict, du: dict, target: str) -> str:  # noqa: PLR0911 - one per step
    """The first step that did not happen, and what it means."""
    if report.success:
        return "The UE completed the handover on the target cell."
    if "decision" not in cucp:
        refusal = f" It answered: {clean(cucp['refusal'])}" if "refusal" in cucp else ""
        hints = [hint for key, hint in REFUSAL_HINTS.items() if key in cucp.get("refusal", "")]
        meaning = f" Meaning: {hints[0]}" if hints else ""
        return f"The CU-CP did not trigger the handover: check the CU UE ID and the telnet answer.{refusal}{meaning}"
    if "preparation" not in du:
        return f"{target} did not prepare the UE (no CFRA process): UE context setup failed on the target."
    if "preamble_line" not in du:
        return (
            f"{target} never received the dedicated preamble {report.preamble}: the UE did not execute the "
            "handover command (not received from the source cell), or its preamble did not reach the target RU."
        )
    if du.get("c_rnti_ce"):
        return _uplink_lost(du, target)
    if du["retransmissions"]:
        return (
            "The UE did not decode the random access response of the target cell: it sent its dedicated "
            "preamble again (downlink of the target cell at the UE)."
        )
    return (
        "The UE accessed the target cell, but its RRCReconfigurationComplete never reached the CU-CP "
        "(uplink of the target cell, or the UE lost the link right after the access)."
    )


def _uplink_lost(du: dict, target: str) -> str:
    """The UE reached the target cell but the DU did not decode it: with OAI, often an SRS configuration mismatch."""
    diagnosis = (
        f"The UE reached the target cell: it sent its new C-RNTI {du['rnti']} to {target} in "
        f"{du['c_rnti_ce']} contention-based random access(es), but {target} did not decode its uplink "
        "afterwards, and its RRCReconfigurationComplete never reached the CU-CP."
    )
    before, after = du.get("srs_ports", (None, None))
    if before is not None and before != after:
        diagnosis += (
            f" {target} prepared the UE with a CellGroup without its capabilities (SRS on {before} port(s)) and "
            f"sent it a CellGroup with its capabilities (SRS on {after} port(s)): OAI keeps scheduling the UE with "
            "the first one after the random access, so the UE and the DU do not use the same uplink configuration."
        )
    return diagnosis
