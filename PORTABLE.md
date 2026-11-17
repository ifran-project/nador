# Portable Extended Observability

These optional probes observe Linux and 5G protocol behavior. Availability
depends on the kernel, radio topology, and enabled traffic.

## Scope

Included: association-scoped F1AP/E1AP observations and matching;
scheduler runnable-wait accounting; network-namespace
drop attribution; observed TCP flow evidence; and measurement quality.

Excluded by request: E2SM-KPM, power/energy, vendor-specific NIC/hardware queue
telemetry and SBI HTTP/2/TLS payload instrumentation. Existing portable Linux
software-qdisc collection remains an optional switch.

## Scientific boundaries

- F1AP/E1AP local IDs are scoped to associations and lifetimes, not globally unique
  subscriber IDs. Do not equate F1AP IDs, RNTIs, NGAP IDs or IMSIs without
  an observed and validated join.
- Setup timing measures the observed signaling exchange at one anchor,
  not complete registration time or UE application latency.
- Packet drop evidence is tied to the observed kernel path. A pod namespace
  does not identify the application instruction that caused a drop.
- TCP transport RTT is not GTP-U/N3 RTT. An idle flow may have no new RTT sample.

## Safety and portability

The node probes require privileged access to selected process/network namespaces.
Kernel tracing may be unsupported on a node; treat that state as unknown,
not healthy zero. Do not change host sysctls to make an experiment pass.
Targets, pending operations and socket identities must be bounded.
Do not export the socket identity key.

No deployment or live kernel validation is implied by offline tests or image builds.
First validate each enabled capability on the target kernel and radio/core layout,
then measure probe CPU, memory, cardinality, event loss and activation overhead.

## Activation and remaining limits

### Independent probe releases

Set `extended.probe_images` instead of the legacy `node_image` and
`service_image` fields. Each enabled purpose needs its own published image.
The matching `probe-<purpose>` release starts and stops independently.
No existing latency probe, AMF sniffer, or SMF sniffer is changed.
The [digest-pinned example](examples/quickstart/dedicated-probes.yaml) enables
only CPU by default; selecting an image does not enable its probe.

| Image suffix after `nador-probe-` | What the release runs |
| --- | --- |
| cpu | Scheduler runnable wait and CPU runtime |
| drops | Kernel drop reasons and optional namespace attribution |
| queueing | Software qdisc backlog |
| tcp | Namespace TCP counters and per-socket evidence |
| split-signaling | F1AP/E1AP observations, context and setup timing |
| measurement-quality | Prometheus availability, finite values, and freshness |

### Passive TCP diagnostics on N3

The experimental latency image reuses the gNB/UPF N3 observation hooks for
tracked IPv4 GTP-U TCP traffic. It exports
`gtp_tcp_retransmission_candidates_total` and `gtp_tcp_resets_observed_total`
by anchor, direction, and available UE/slice metadata. No UE or traffic-server
agent is required. The existing TCP data/ACK latency measurements are unchanged.

The `gtp_tcp_rtt_variation_seconds` histogram measures the absolute change
between consecutive accepted data-to-ACK RTT samples of one flow. It uses
exact ACK matches and skips known repeated data ranges. A tunnel change or
gap longer than five seconds starts a new comparison. Flow identifiers stay
internal; the metric works without the packet pairer. It is not one-way jitter
or the endpoint TCP RTT estimator.

A candidate repeats the same sequence start and payload length on the same
directional inner flow and receiving GTP-U endpoint/TEID within a bounded
five-second history. The table holds four entries per hash bucket.
Report this metric as **N3-observed retransmission candidates**, not total
UE TCP retransmissions. If a packet is dropped before N3, a retry can appear
there as the first copy.
`gtp_tcp_repeat_history_evictions_total` reports still-recent entries
replaced under pressure; an eviction is not itself a missed retransmission.
Capture loss, segmentation/offload, eviction, and
duplicate packets can affect the count. Do not sum anchors as distinct
retransmissions or interpret the result as a loss percentage. A reset is an
observed RST packet, not necessarily a failed connection. These counters
create a label series only after its first event; check capture coverage
before interpreting an absent series.

The node diagnostics collector separately reports
`nador_qdisc_dropped_packets_total` when the Linux software queue exposes a
drop counter. Queue backlog and drops cover all traffic traversing that queue,
including forwarded 5G traffic when it uses the monitored interface. They
do not distinguish UEs, slices, or protocols. Kernel drop attribution remains
separate and best-effort.

For example, enable `scheduler_wait`, set `probe_images.cpu` to the published CPU
image, and set `probe_targets.cpu: ["core/upf"]` using your deployment's
component name. Optional `probe_targets.<purpose>` lists select `category/name`
targets; omitting one uses the normal discovery set. A DaemonSet can still
run on multiple nodes. Drop tracing observes the local kernel path and the
selected targets constrain attribution, not which kernel drop events occur.

On a running deployment, after configuring the optional releases and calling
`monitoring.plan()`, use the existing selected-component actions:

```python
await monitoring.install(components=["probe-cpu"])
await monitoring.uninstall(components=["probe-cpu"])
```

Do not run both a legacy bundled release and independent releases for the
same measurements. Uninstall the bundled release before migrating those probes.
Removing a probe does not uninstall the NF or any other probe release.

### Probe-specific settings

The `attributed_drops` and `tcp_flows` switches are off by default. Use a newly built image from this branch when enabling
any live probe. The previous branch's images do not contain these collectors.

For TCP flow evidence, select an existing Secret in the monitoring namespace
with a `key` value of at least 32 characters, and set `packet_identity_secret`
and a unique `packet_run_id`. Only the Secret name is rendered in Helm; its
value is not written to experiment reports.

F1AP/E1AP provides association-local context and setup timing, not a verified
cross-protocol subscriber graph.
