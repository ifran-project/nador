# ML observability migration inventory

This inventory records
what exists in the older implementation and the boundaries for adapting it.
The collection layer is now adapted in this checkout; see `OBSERVABILITY.md`.
Selected images are published. OAI single-gNB and multi-gNB RFsim validation
passed; ZeroMQ complete artifact export and physical-RU validation remain pending.

Runtime image sources now live in the sibling `nador-images` checkout on
its local `ml-observability` branch: the existing AMF/SMF/mapper component
directories, `common/observation_runtime.py`, and the new `user-plane-path/`.
NADOR retains deployment settings and artifact collection only. Duplicate
runtime modules and the temporary image-context generator were removed here.
The image repository includes Docker recipes, collector tests, build-matrix
registration and versioned source records. Published references and digests
are selected by NADOR manifests; the path probe remains explicitly opt-in.

## Research objective

Collect live 5G signaling, user-plane, UE/slice/session context, infrastructure
metrics and logs; then evaluate whether combining them improves fault
localization and identification of affected UEs. ML diagnosis, fault datasets
and evidence-based LLM explanations are subsequent research stages.

## Existing implementations

The old deployment integration lives in `5g_ansible-develop-ML`. Its later
orchestrator migration lives in the sibling `orchestrator-monitoring-ML`.

| Capability | What was implemented | Evidence and limits |
| --- | --- | --- |
| Mapper health/coverage | Bounded Redis reads; scrape/Redis health, collection duration, context counts, identified UEs, paired contexts by slice and missing-field counts | Coverage metrics, not authoritative UE activity or freshness; new scoped mapper keys must be considered |
| N2 procedure metrics | NGAP PDU-session resource setup request/response counters, outcome categories, histogram timings, pending/expired/unmatched/untrackable/overlap/decode counters | Strict AMF/RAN UE identifiers and session-set matching; not all NGAP procedures or complete end-to-end PDU-session establishment |
| N4 procedure metrics | PFCP session establishment request/response counters, Cause outcomes, histogram timings and correlation/capture diagnostics | Exact sequence plus observed peer endpoints; local request-response timing, not overall session or user-plane latency |
| gNB/UPF path probe | Interface RX/TX bytes, packets, errors and drops; GTP-U packet/byte observations, capture state and last-seen time | External pod-network observer; interface drops do not establish end-to-end packet loss |
| Path selection | Explicit interfaces/network names, N3-like attachment hints and GTP-U confirmation; N6 default-route selection | Idle capture can remain provisional; shared interfaces do not isolate protocols; multiple candidates can be ambiguous |
| Infrastructure metrics | Enabled standard node/container/Kubernetes telemetry and its experiment queries, including scheduling/pressure, throttling, resources, networking and clocks | Mostly existing exporters rather than newly written probes; actual availability depends on host/kernel/exporter settings |
| Setup artifacts | Before/after host and cluster snapshots: hardware, kernel, interfaces/routes, pod identities, image IDs, resource configuration and ConfigMap fingerprints | Read-only metadata collection; fingerprints rather than raw ConfigMap contents |
| UE context history | Bounded periodic mapper inventory snapshots, timestamps, sampling errors, truncation hints and final summaries | Sampled history, not a complete event log; transient changes between polls can be missed |
| Experiment collection | Extended native experiment windows with metric exports, setup/UE context and optional bounded Loki logs; cleanup on failure/cancellation | Workload exceptions preserved; incomplete collection recorded; full Loki pages flagged as possibly truncated |
| Availability check | Read-only Prometheus/Loki checks, scrape target health and available/partial/non-finite/empty query states | Empty results remain unknown or conditional, not healthy zero |
| Deployment integration | Typed opt-in collector settings, sniffer environment/ports, component targeting, per-pod scraping, restart/cleanup, image build sources | Old orchestrator model/controller integration cannot be copied unchanged into NADOR |
| Trial sync | Local three-way source synchronization against the trial checkout | Obsolete for the current independent-extension architecture; do not port it |

## What was tested

Earlier deployment messages show successful gNB/UPF GTP-U observations and NGAP
setup counters in Prometheus. They do not establish complete live validation of
every collector or full experiment collection.

The older orchestrator migration's recorded local result was 79 passing tests
plus 15 subtests, Ansible syntax/template checks and a package build. Those
tests used mocked cluster boundaries/local fixtures. That migration did not
build/publish new images or deploy its own live cluster. These historical
results are not a current NADOR compatibility result.

## Current NADOR baseline

NADOR already provides Prometheus/Grafana/Loki, current UE/session/tunnel
mapping, eBPF latency and N3 throughput, Helm lifecycle management, and
ExperimentRun timelines/reports/metric export with automatic Prometheus access.
Use these existing pieces as the integration points.

The newer session identity includes gNB scope and endpoint-scoped SMF/UPF SEIDs.
Old mapper/sniffer implementations must not overwrite these improvements.
Current N3 throughput is also richer than interface byte counters; keep both
measurements distinct rather than replacing it with the old path observer.

## Adaptation order

1. Extend the existing ExperimentRun with opt-in setup and sampled UE-context
   collection, using the monitoring manager's connection/model information.
   Retain the richer current mapper/session fields. Add bounded Loki export and
   the availability/query tools through the existing artifact package.
2. Adapt mapper-coverage and NGAP/PFCP collectors to the current component image
   sources and schemas. Preserve current mapping logic, expose explicit image
   requirements, and prepare image-source changes before any build or publish.
3. Add optional path/host telemetry through NADOR models and owned Helm/
   Ansible assets. Integrate per-pod scrape discovery and existing cleanup.
4. Validate locally, then perform a live baseline smoke test with fresh UE
   attachment before controlled faults or model training.

All work belongs in the extension or its own component sources. Keep the
upstream orchestrator unchanged. Keep private tests/trials/results local and
explain intended edits before changing production behavior. No pushes, image
publishing, tags or releases without explicit user confirmation.

## Local port validation

The focused NADOR lifecycle/access/cleanup and new observability suite
passes 33 tests, including Helm rendering with/without path probes. The
generated current-source image contexts compile and a synthetic Scapy PFCP
exchange passes through the new observation hook. Package build succeeds.

The existing network-overlay suite has 24 failures on both the unchanged
checkout and this port: the installed upstream model lacks the custom chart/
rendered-file fields expected by those notebook tests. This pre-existing local
dependency mismatch is separate from collector validation. No live cluster or
new image has been tested by this port yet.

## Still outstanding from the original research plan

- Active pod-to-pod connectivity checks and related anomaly detection.
- A systematic controlled-fault dataset with reliable injection labels.
- A trained and evaluated ML fault-diagnosis model and comparative baselines.
- An evaluated LLM diagnostic assistant grounded in collected evidence.
- Demonstrated cross-stack diagnostic accuracy and monitoring overhead.
