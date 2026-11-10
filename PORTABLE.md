# Portable Extended Observability

Implementation branch: `ml-extended-portable` in NADOR and NADOR Images,
based on `ml-extended`. Existing ML and base monitoring remain unchanged.
This is experimental instrumentation, not a claim of universally supported
Linux or 5G measurements.

## Scope

Included: sampled session validity within traffic windows; association-scoped
F1AP/E1AP observations and matching; targeted off-CPU analysis; network-namespace
drop attribution; GTP-U QFI; observed TCP flow evidence; explicitly matched
cross-anchor packet evidence; allowlisted source-NF reachability.

Excluded by request: E2SM-KPM, power/energy, vendor-specific NIC/hardware queue
telemetry and SBI HTTP/2/TLS payload instrumentation. Existing portable Linux
software-qdisc collection remains an optional switch.

## Scientific boundaries

- A sampled stable session is not proof that no transition happened between polls.
- F1AP/E1AP local IDs are scoped to associations and lifetimes, not globally unique
  subscriber IDs. Do not equate F1AP IDs, RNTIs, NGAP IDs or IMSIs without
  an observed and validated join.
- Setup timing measures the observed signaling exchange at one anchor,
  not complete registration time or UE application latency.
- Off-CPU includes both runnable and blocked/sleeping intervals. An interval
  or a stack sample alone is not an identified root cause.
- Packet drop evidence is tied to the observed kernel path. A pod namespace
  does not identify the application instruction that caused a drop.
- TCP transport RTT is not GTP-U/N3 RTT. An idle flow may have no new RTT sample.
- A QFI label is not proof of QoS enforcement or a validated radio bearer join.
- Unmatched packets may result from sampling, offloads, missed observations,
  expiry or path changes. Do not label every unmatched observation as loss.
- TCP/SCTP connection checks and PFCP heartbeat checks have different meanings.
  None proves successful NGAP registration or PDU-session establishment.

## Safety and portability

The node probes require privileged access to selected process/network namespaces.
Kernel tracing may be unsupported on a node; treat that state as unknown,
not healthy zero. Do not change host sysctls to make an experiment pass.
Targets, pending operations and packet identities must be bounded.
Raw packet payloads and the shared packet-identity key must not be exported.
Only explicitly allowlisted active checks may send traffic.

No deployment or live kernel validation is implied by offline tests or image builds.
First validate each enabled capability on the target kernel and radio/core layout,
then measure probe CPU, memory, cardinality, event loss and activation overhead.

## Activation and remaining limits

All new switches are off by default: `session_window_validation`, `off_cpu`,
`off_cpu_stacks`, `attributed_drops`, `qfi`, `tcp_flows`, and
`packet_concordance`. Use a newly built image from this branch when enabling
any live probe. The previous branch's images do not contain these collectors.

For QFI, socket, or packet evidence, select an existing Secret in the monitoring
namespace with a `key` value of at least 32 characters, and set
`packet_identity_secret`, a unique `packet_run_id`, and the actual capture
`packet_interfaces`. Only the Secret name is rendered in Helm; its value
is not written to experiment reports.

The QFI collector reports observed QFI counts, not a validated QFI-to-UE or DRB
join. Concordance currently compares distinct packet identities across sources
observed by the same node collector; cross-node aggregation is not implemented.
F1AP/E1AP provides association-local context and setup timing, not a verified
cross-protocol subscriber graph. Kernel stack IDs are bounded and unsymbolized.

Source-NF checks require explicit destination IP pins and support TCP/SCTP
connectivity only. Synthetic PFCP heartbeats are restricted to a collector
and an explicitly designated isolated test peer, never a real NF association.
Session-window analysis writes `session_window_validity.json` from sampled mapper
history and the traffic timeline; it does not override the trial runner's
strict post-export mapping check.
