# Extended ML Observability

This branch adds opt-in experimental collectors without changing the original
ML switches, default probe images, upstream orchestrator, or 5G binaries.
The same branch name, `ml-extended`, exists in `nador-images`.

## Implemented for initial testing

| Switch | Collection | Interpretation and limitations |
| --- | --- | --- |
| scheduler_wait | All discovered NF threads' Linux schedstat runtime and runnable wait snapshots | Kernel accounting must be enabled. Snapshot can decrease when threads exit; do not apply rate() as if it were a monotonic pod counter. This is not off-CPU blocking or stack attribution. |
| kernel_drops | BCC skb:kfree_skb tracepoint numeric reason counts | Node-scoped, kernel-local reason codes, not per-NF attribution or complete packet loss. Requires compatible headers/tracepoint/BPF privileges. |
| network_queues | Per-pod netns tc JSON software-qdisc backlog | Does not cover hardware queues, DPDK, VPP or XDP bypass. |
| socket_counters | Per-pod netns TCP retransmission/reset/failure counters | Namespace counters; not per-flow RTT or GTP-U tunnel measurements. |
| split_signaling | Bounded tshark F1AP/E1AP PDU counts and last observation per selected RAN pod | Does not yet reconstruct UE context or correlate requests/responses. No IMSI/RNTI/NCGI joins or latency claims. Both anchors may observe the same PDU. |
| quality | Configured Prometheus query nonemptiness, finite values and optional event timestamp freshness | A scrape timestamp is not an event timestamp. Missing/failed queries are not healthy zeros. |
| reachability | Allowlisted TCP connection attempts | Runs from the service collector pod, not from every NF. Does not validate SCTP, UDP, PFCP, NGAP or SBI transaction semantics. |

Collectors expose Prometheus metrics on ports 9500 and 9501 through the existing
annotated-pod discovery. A separate Helm release installs and removes them.
Polling is independent of scraping, and cached values expire if polling stops.
The node collector reads only selected pod identities but needs cluster-wide
pod list permission and privileged host PID/network-namespace access.
This is intended for a trusted experimental cluster, not an unreviewed
production deployment. Scheduling overhead and decoder CPU must be measured.

## Images and activation

The images workflow builds from Debian/Python bases, not inherited probe
images. Only the `ml-extended` workflow publishes `extended-<commit>` tags;
it never updates main's image pins. After a successful build, use the two exact
image tags or digests in the monitoring profile:

```yaml
extended:
  scheduler_wait: true
  kernel_drops: true
  network_queues: true
  socket_counters: true
  split_signaling: true
  quality: true
  node_image: ghcr.io/yassir63/nador-extended-node:extended-REPLACE_WITH_PUBLISHED_COMMIT
  service_image: ghcr.io/yassir63/nador-extended-service:extended-REPLACE_WITH_PUBLISHED_COMMIT
  quality_queries:
    - name: user_plane_events
      query: user_plane_probe_gtpu_last_packet_timestamp_seconds
      kind: event_timestamp
      max_age_seconds: 30
```

Do not run the example with placeholders. Image selection is mandatory:
no unpublished default image is silently installed. Leave `extended` absent
for unchanged monitoring behavior. For an existing deployment, remove the
`extended-probes` Helm release before disabling it; switches do not undo
previously installed releases automatically.

The main measurement-quality gap still requiring implementation is historical
session validity over the traffic window. These quality metrics alone do not
fix the post-export session identity comparison in the private trial runner.

## Not yet implemented

The research note is a roadmap, not a list of finished probes. Full F1AP/E1AP
context reconstruction, association-scoped procedure timing, off-CPU stacks,
QFI enrichment, per-flow TCP RTT, driver-specific NIC telemetry, energy
attribution, E2SM-KPM adapters, SBI HTTP/2/TLS instrumentation and cross-anchor
packet concordance remain separate implementation and validation tasks.
Do not present them as available from this branch.

## Validation

Start offline: models and chart rendering, thread/cgroup parsing fixtures,
decoder PDML fixtures, finite/stale/missing quality cases and TCP connectivity.
GitHub then builds both images and checks packaged dependencies.
Only then test in a Linux cluster: scheduling contention, controlled qdisc/drop
faults, F1/E1 session procedures, and bounded reachability failures.
Capture kernel/config versions, image digests, overhead and event-loss bounds.
No live validation or image publication is claimed by adding this source.

Primary references:
- https://docs.kernel.org/scheduler/sched-stats.html
- https://github.com/torvalds/linux/blob/master/include/trace/events/skb.h
- https://www.wireshark.org/docs/dfref/f/f1ap.html
- https://www.wireshark.org/docs/dfref/e/e1ap.html
