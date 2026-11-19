"""
Trigger an F1 handover between two DUs of the same CU-CP and check it, step by step, in the CU-CP and DU logs.

The F1 handover (intra-CU, inter-DU mobility, 3GPP TS 38.401 section 8.2.1.1)
==============================================================================

A UE served by a source DU moves to a cell of a target DU; both DUs are connected over F1 to the same CU-CP, which
keeps the UE context (RRC, PDCP, NGAP, the PDU sessions): the AMF and the UPF see nothing. The CU-CP drives the
procedure; the two DUs only prepare and release the lower layers (RLC, MAC, PHY) of the UE.

::

    UE            source DU            target DU               CU-CP            CU-UP
    |                 |                    |                     |                 |
    | (1) decision: measurement report, or a manual command on the CU-CP           |
    |                 |                    |<-- UE CONTEXT ------|                 |
    |                 |                    |    SETUP REQUEST    |  (2) prepare    |
    |                 |                    |--- UE CONTEXT ----->|  the target     |
    |                 |                    |    SETUP RESPONSE   |                 |
    |                 |                    |  (new C-RNTI,       |-- E1 BEARER --->|
    |                 |                    |   CellGroupConfig,  |   CONTEXT MOD.  |
    |                 |                    |   dedicated preamble)  (F1-U to target DU)
    |                 |<-- UE CONTEXT MODIFICATION REQUEST ------|                 |
    |<- RRCReconfiguration (reconfigurationWithSync) -- (3) handover command       |
    |                 |                    |                     |                 |
    |-- (4) random access on the target cell: preamble (Msg1) -->|                 |
    |<- (5) random access response (Msg2) ---|                   |                 |
    |-- (6) RRCReconfigurationComplete --->|-- UL RRC MESSAGE -->|                 |
    |                 |                    |   TRANSFER          |                 |
    |                 |<-- (7) UE CONTEXT RELEASE COMMAND -------|                 |

1. **Decision.** The CU-CP decides the handover, from a measurement report of the UE (event A3: a neighbour cell
   becomes better than the serving cell) or, in these experiments, from a manual command: ``ci trigger_f1_ho <CU UE
   ID>`` on the OAI CU-CP telnet server, ``ho <serving PCI> <C-RNTI> <target PCI>`` on the OCUDU CU-CP console.
2. **Preparation of the target DU.** F1AP UE CONTEXT SETUP REQUEST: the target DU creates the UE, allocates it a new
   C-RNTI and, with contention-free random access (CFRA), a dedicated preamble, and returns the radio configuration
   of the UE in its cell (CellGroupConfig). The CU-CP moves the F1-U tunnels of the PDU sessions to the target DU
   (E1AP BEARER CONTEXT MODIFICATION with the CU-UP).
3. **Handover command.** The CU-CP builds the RRCReconfiguration with reconfigurationWithSync (target cell, new
   C-RNTI, dedicated preamble, CellGroupConfig of the target DU) and sends it to the UE through the source DU (F1AP
   UE CONTEXT MODIFICATION REQUEST, or DL RRC MESSAGE TRANSFER); the source DU stops scheduling the UE.
4. **Random access on the target cell (Msg1).** The UE synchronises on the SSB of the target cell and sends its
   preamble: the dedicated one with CFRA (the target DU knows the UE at once), any preamble otherwise (the UE then
   identifies itself in Msg3 with its new C-RNTI).
5. **Random access response (Msg2).** The target DU answers with a timing advance and an uplink grant. If the UE
   does not decode it, it sends its preamble again (power ramping): the target DU sees the same preamble again.
6. **Completion.** The UE sends RRCReconfigurationComplete on the target cell; the target DU forwards it to the CU-CP
   (F1AP UL RRC MESSAGE TRANSFER). This is the only proof that the UE reached the target cell: the handover is a
   success when the CU-CP receives it from the target DU.
7. **Release of the source DU.** The CU-CP releases the UE context of the source DU (F1AP UE CONTEXT RELEASE
   COMMAND); the UE now exchanges data through the target DU.

If the UE does not complete in time (timer T304 of the UE, set by the CU-CP in the handover command), the handover
fails: the UE looks for a cell and requests an RRC re-establishment (on the target or the source cell), or falls back
to a new attachment. The CU-CP may also cancel the handover and keep the UE on the source DU (rollback).

How this module checks it
=========================

:func:`run_handover` sends the manual command to the CU-CP, waits, reads the logs of the CU-CP and of both DUs (and
the previous logs of a DU container that restarted meanwhile), and gives them to the analysis of the RAN
implementation of the CU-CP (:mod:`nador.handover.oai` or :mod:`nador.handover.ocudu`). The analysis looks, for each
step above, for the log line that proves it, and returns a :class:`HandoverReport`: each step with what it means, the
log line found (or why it is missing), a diagnosis of the first missing step, what followed a failure, and where the
UE is served afterwards. :meth:`HandoverReport.to_text` prints it.
"""

from nador.handover.oai import analyze_oai_handover
from nador.handover.ocudu import analyze_ocudu_handover
from nador.handover.report import PROCEDURE, HandoverReport, Step
from nador.handover.run import run_handover

# Former name of the OAI analysis, kept for the notebooks and scripts that use it.
analyze_handover = analyze_oai_handover

__all__ = [
    "PROCEDURE",
    "HandoverReport",
    "Step",
    "analyze_handover",
    "analyze_oai_handover",
    "analyze_ocudu_handover",
    "run_handover",
]
