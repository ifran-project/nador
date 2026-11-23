"""The handover report: each step of an F1 handover found in the CU-CP and DU logs of OAI and OCUDU."""

import json

import pytest
import support
from ifran.deployment import ProvisioningManagement
from ifran.deployment.base import BaseManagement

from nador.handover import HandoverWatch, oai, ocudu

# OAI: the CU-CP log has no timestamps, the DU lines start with a time in seconds.
OAI_ATTACH = (
    "[NR_RRC]   (cellID 1, UE ID 1 RNTI 7e3e) Create UE context: CU UE ID 1 DU UE ID 1\n"
    "[NR_RRC]   UE 1: created new DRB 1\n"
    "[NR_RRC]   (cellID 1, UE ID 2 RNTI 51a0) Create UE context: CU UE ID 2 DU UE ID 2\n"
)
OAI_TRIGGERED = (
    "[TELNET]   ci trigger_f1_ho 1\n"
    "[NR_RRC]   Handover triggered for UE 1/RNTI 7e3e towards cell 2/assoc_id 5/PCI 2\n"
    "[NR_RRC]   HO acknowledged: Send reconfiguration for UE 1/RNTI 7e3e\n"
    "[NR_RRC]   UE 1: update CU F1AP Context DU UE ID 1 => 2 and RNTI 7e3e => 9003\n"
)
OAI_COMPLETED = (
    "[NR_RRC]   (cellID 2, UE ID 1 RNTI 9003) Received RRCReconfigurationComplete\n"
    "[NR_RRC]   handover for UE 1/RNTI 9003 complete!\n"
    "[NR_RRC]   UE 1 Handover: trigger release on cell PCI 1\n"
)
OAI_TARGET_PREPARED = (
    "2098.100 [NR_MAC] SRS configured with 1 ports\n"
    "2098.200 [NR_MAC] Added new CFRA process for UE RNTI 9003 with preamble(s): 63\n"
    "2098.300 [NR_MAC] SRS configured with 2 ports\n"
    "2098.400 [NR_MAC] TC-RNTI 9003: initiating RA procedure for RAPID 63\n"
    "2098.500 [NR_MAC] (rnti 0x9003) CFRA procedure succeeded!\n"
)
OAI_SERVED = "2099.000 [NR_MAC] UE RNTI 9003 PCI 2 CellID 2 CU-UE-ID 1 in-sync F PH 30 dB\n"


def oai_report(cucp, target_du, source_du=""):
    return oai.analyze_oai_handover(1, "du1", "du2", OAI_ATTACH + cucp, {"du1": source_du, "du2": target_du})


def marks(report):
    return {step.key: step.ok for step in report.steps}


def test_an_oai_handover_seen_at_every_step():
    report = oai_report(OAI_TRIGGERED + OAI_COMPLETED, OAI_TARGET_PREPARED + OAI_SERVED)
    assert report.success
    assert set(marks(report).values()) == {True}
    assert (report.target_cell, report.preamble) == (2, 63)
    assert report.rntis == {"source": "7e3e", "target": "9003", "chain": "7e3e 9003"}
    assert report.ue_location == {"du1": "not served", "du2": "PH 30 dB"}
    text = report.to_text()
    assert text.startswith("Handover of CU UE ID 1 (OAI): du1 -> du2: SUCCESS")
    assert "  6. [x] " in text
    assert "log: [NR_RRC]   (cellID 2, UE ID 1 RNTI 9003) Received RRCReconfigurationComplete" in text


def test_an_oai_ue_on_the_target_cell_whose_uplink_is_not_decoded():
    target = OAI_TARGET_PREPARED + "2098.900 [NR_MAC] Received a MAC CE for C-RNTI with 9003\n"
    target += "2099.100 [NR_MAC] UE 9003: ulsch_rounds 10/0/0/0, ulsch_errors 10, ulsch_DTX 4\n"
    report = oai_report(OAI_TRIGGERED, target)
    assert not report.success
    assert marks(report)["completion"] is False
    assert "it sent its new C-RNTI 9003 to du2 in 1 contention-based" in report.diagnosis
    assert "(SRS on 1 port(s))" in report.diagnosis
    (uplink,) = report.aftermath
    assert uplink.startswith("du2: uplink of RNTI 9003: ")
    assert uplink.endswith("ulsch_errors 10, ulsch_DTX 4")


def test_an_oai_ue_that_sends_its_preamble_again_and_re_establishes():
    target = OAI_TARGET_PREPARED + "2098.600 [NR_MAC] ignoring RAPID 63\n2098.700 [NR_MAC] ignoring RAPID 63\n"
    cucp = OAI_TRIGGERED + "[NR_RRC]   Reestablishment RNTI 4b21 req C-RNTI 7e3e physCellId 1\n"
    cucp += "[NR_RRC]   handover for UE 1/RNTI 7e3e failed, rollback\n"
    report = oai_report(cucp, target)
    assert report.step("response").detail == "then the preamble came again 2 time(s)"
    assert report.diagnosis.startswith("The UE did not decode the random access response")
    assert report.aftermath == [
        "the UE requested an RRC re-establishment (RNTI 4b21, from C-RNTI 7e3e, PCI 1)",
        "CU-CP: handover failed, the UE is kept on the source cell (rollback)",
    ]


@pytest.mark.parametrize(
    ("cucp", "target", "diagnosis"),
    [
        (OAI_TRIGGERED, "", "du2 did not prepare the UE"),
        (OAI_TRIGGERED, OAI_TARGET_PREPARED.partition("2098.400")[0], "du2 never received the dedicated preamble"),
        (OAI_TRIGGERED, OAI_TARGET_PREPARED, "The UE accessed the target cell, but its RRCReconfigurationComplete"),
    ],
)
def test_where_an_oai_handover_stopped(cucp, target, diagnosis):
    assert oai_report(cucp, target).diagnosis.startswith(diagnosis)


def test_an_oai_refusal_is_explained():
    cucp = "[TELNET]   ci trigger_f1_ho 1\n[NR_RRC]   cannot trigger handover for UE 1: no DRB configured\n"
    report = oai_report(cucp, "")
    assert "It answered: [NR_RRC]   cannot trigger handover for UE 1: no DRB configured" in report.diagnosis
    assert "Meaning: the UE has no data radio bearer" in report.diagnosis


def test_the_oai_ues_a_cu_cp_serves():
    log = OAI_ATTACH + OAI_TRIGGERED + "[NR_RRC]   removed UE CU UE ID 2/RNTI 51a0\n"
    assert oai.connected_ues(log) == [{"ue": 1, "rnti": "9003", "cell": 1, "drb": True}]


# OCUDU: F1AP messages per UE index of the CU-CP; the C-RNTI changes on the target DU.
OCUDU_ATTACH = (
    "[CU-F1] Rx PDU du=1: InitialULRRCMessageTransfer\n"
    "[RRC] ue=8 c-rnti=0x4601: CCCH UL rrcSetupRequest\n"
    "[RRC] ue=9 c-rnti=0x4602: CCCH UL rrcSetupRequest\n"
    '[CU-CP] ue=9: "UE Removal Routine" finished successfully\n'
)
OCUDU_PREPARED = (
    "[CU-CP] ue=8: Trigger intra-CU (inter-DU) handover from source_du=0 to target_du=1\n"
    "[CU-F1] Tx PDU du=2 ue=15: UEContextSetupRequest\n"
    "[CU-F1] Rx PDU du=2 ue=15: UEContextSetupResponse\n"
    "[RRC] ue=15 c-rnti=0x4701: UE created\n"
    "[CU-F1] Tx PDU du=1 ue=8: UEContextModificationRequest\n"
)
OCUDU_COMPLETED = (
    "[RRC] ue=15 c-rnti=0x4701: DCCH UL rrcReconfigurationComplete\n"
    "[CU-F1] Rx PDU du=2 ue=15: ULRRCMessageTransfer\n"
    "[CU-F1] Tx PDU du=1 ue=8: UEContextReleaseCommand\n"
)
OCUDU_TARGET = (
    "[F1AP] UEContextSetupRequest received\n"
    '[MAC] ue=0 crnti=0x4701 proc="MAC UE Creation": finished successfully\n'
    "[SCHED] Processed slot events pci=2: prach(ra-rnti=0x10b preamble=52 ssb=0 tc-rnti=0x4701)\n"
)


def ocudu_report(cucp, target, answer=""):
    return ocudu.analyze_ocudu_handover(
        0x4601, "du1", "du2", OCUDU_ATTACH + cucp, {"du1": "", "du2": target}, target_pci=2, answer=answer
    )


def test_an_ocudu_handover_seen_at_every_step():
    report = ocudu_report(OCUDU_PREPARED + OCUDU_COMPLETED, OCUDU_TARGET)
    assert report.success
    assert marks(report) == {
        "decision": True,
        "preparation": True,
        "command": True,
        "preamble": True,
        "response": None,
        "completion": True,
        "release": True,
    }
    assert (report.rntis, report.preamble) == ({"source": "0x4601", "target": "0x4701"}, 52)
    assert report.step("preparation").detail == "target UE ue=15, new C-RNTI 0x4701"


def test_an_ocudu_target_log_that_does_not_cover_the_handover():
    report = ocudu_report(OCUDU_PREPARED + OCUDU_COMPLETED, "")
    assert report.success
    assert report.step("preamble").ok is None


@pytest.mark.parametrize(
    ("cucp", "target", "diagnosis"),
    [
        (OCUDU_PREPARED.replace("UEContextSetupResponse", "UEContextSetupFailure"), "", "du2 did not accept the UE"),
        (OCUDU_PREPARED.split("[CU-F1] Tx PDU du=1", maxsplit=1)[0], "", "The CU-CP did not send the handover command"),
        (OCUDU_PREPARED, "", "No random access on du2 after the handover command"),
        (OCUDU_PREPARED, OCUDU_TARGET + OCUDU_TARGET.splitlines()[-1], "The UE did not decode the random access"),
        (OCUDU_PREPARED, OCUDU_TARGET, "The UE was prepared and commanded"),
    ],
)
def test_where_an_ocudu_handover_stopped(cucp, target, diagnosis):
    assert ocudu_report(cucp, target).diagnosis.startswith(diagnosis)


def test_an_ocudu_refusal_and_what_followed():
    answer = '{"error": "CU-CP rejected trigger_handover: unknown UE"}'
    report = ocudu_report("", "", answer=answer)
    assert f"It answered: {answer}" in report.diagnosis
    cucp = OCUDU_PREPARED + "[CU-F1] Tx PDU du=2 ue=15: UEContextReleaseCommand\n"
    cucp += "[RRC] ue=16 c-rnti=0x4602: CCCH UL rrcReestablishmentRequest\n"
    assert ocudu_report(cucp, OCUDU_TARGET).aftermath == [
        "CU-CP released the UE on the target DU (du=2): handover cancelled",
        "a UE requested an RRC re-establishment (rrcReestablishmentRequest)",
    ]


def test_the_ocudu_ues_a_cu_cp_serves():
    log = OCUDU_ATTACH + OCUDU_PREPARED
    assert ocudu.connected_ues(log) == [
        {"ue": 8, "du": 1, "c_rnti": "0x4601"},
        {"ue": 15, "du": 2, "c_rnti": "0x4701"},
    ]
    assert ocudu.format_rnti("4601") == ocudu.format_rnti(17921) == "0x4601"


# Watching a handover on a deployed RAN


@pytest.fixture
def two_dus(monkeypatch):
    """An OAI CU-CP with two DUs, whose DU2 container restarts during the handover."""
    layout = support.RAN_LAYOUTS["cucp-cuup-du"]
    du2 = layout["du"] | {"config": layout["du"]["config"] | {"gnbDuId": 2, "nrCellId": 2}}
    monkeypatch.setitem(support.RAN_LAYOUTS, "2du", {**layout, "du1": layout["du"], "du2": du2})
    commands = []
    pods = [{"oai-du2-0": {"du": 0, "metrics": 0}}, {"oai-du2-0": {"du": 1, "metrics": 3}}]

    async def run_node_command(self, *, command, nodes, **_):
        commands.append((nodes, command))
        if command[-1] == "--previous":
            return OAI_TARGET_PREPARED
        state = pods.pop(0)
        items = [
            {
                "metadata": {"name": pod},
                "status": {"containerStatuses": [{"name": c, "restartCount": n} for c, n in s.items()]},
            }
            for pod, s in state.items()
        ]
        return json.dumps({"items": items})

    async def get_logs(self, *, components, **_):
        logs = {"cucp": OAI_ATTACH + OAI_TRIGGERED + OAI_COMPLETED, "du1": "", "du2": OAI_SERVED}
        return {name: {"cucp" if name == "cucp" else "du": logs[name], "metrics": "noise"} for name in components}

    monkeypatch.setattr(ProvisioningManagement, "run_node_command", run_node_command)
    monkeypatch.setattr(BaseManagement, "get_logs", get_logs, raising=False)
    net = support.network(layout="2du")
    del net.deployment_model.ran.components["du"]
    return BaseManagement(metamodel=net.metamodel, deployment_model=net.deployment_model), commands


async def test_a_watch_reads_the_log_of_a_du_that_restarted(two_dus, capsys):
    ran, commands = two_dus
    watch = await HandoverWatch(ran, source="du1", target="du2").start()
    report = await watch.report(1)
    assert report.success
    assert report.restarts == {"du2/du": 1}
    assert report.aftermath == ["du2/du restarted 1 time(s)"]
    assert commands[-1] == (
        ["node1"],
        [
            "kubectl",
            "--kubeconfig",
            "/etc/kubernetes/admin.conf",
            "-n",
            "ran",
            "logs",
            "oai-du2-0",
            "-c",
            "du",
            "--previous",
        ],
    )
    assert capsys.readouterr().out.startswith("Handover of CU UE ID 1 (OAI): du1 -> du2: SUCCESS")


async def test_a_watch_must_start_before_the_handover(two_dus):
    ran, _ = two_dus
    with pytest.raises(RuntimeError, match="Call start"):
        await HandoverWatch(ran, source="du1", target="du2").report(1)
    with pytest.raises(ValueError, match="Unknown RAN components: \\['du3'\\]"):
        HandoverWatch(ran, source="du1", target="du3")
