# Handover report

NADOR checks an F1 handover between two DUs of the same CU-CP (OAI or OCUDU) step by step, in the CU-CP and DU
logs. It does not trigger the handover: you trigger it on the CU-CP, and NADOR reports what happened.

```python
from nador.handover import HandoverWatch

watch = await HandoverWatch(ran, source="du1", target="du2", cucp="cucp").start()
# trigger the handover on the CU-CP:
#   OAI:   ci trigger_f1_ho <CU UE ID>  on the CU-CP telnet server
#   OCUDU: a trigger_handover JSON command on the CU-CP remote control server
report = await watch.report(ue)
```

`start` records the restart count of every RAN container, so that `report` also reads the previous log of a DU
container that restarted during the handover. `ue` is the CU UE ID with OAI and the C-RNTI in the source cell
(`0x4601`) with OCUDU; `nador.handover.oai.connected_ues` and `nador.handover.ocudu.connected_ues` list them from the
CU-CP log.

The report follows the procedure: target DU preparation, handover command, random access on the target cell,
completion and release of the source DU. Each step says whether it was seen, what it means and the log line that
proves it; `report.to_text(explain=False)` keeps only the outcome. The handover is a success when the CU-CP receives
RRCReconfigurationComplete from the target DU. The procedure itself, and how each step is found in the logs of each
RAN, are described in the API reference of `nador.handover`.
