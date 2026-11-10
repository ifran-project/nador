# ML observability in NADOR

The upstream orchestrator is unchanged.
Existing monitoring remains unchanged unless these collectors are enabled.

## Selected published builds

Collector defaults use exact published commit tags rather than moving version
tags. Per-component source commits and digests are recorded in
`src/nador/assets/observability_images.json`. Controller, calculator and
probe defaults are recorded in `src/nador/assets/images.json`.
The controller rebuild preserves the recovered hackathon Python dependencies.
The selected latency probe remains the recorded original
`r2labuser/ebpf-latency-probe:2026w43`; an equivalent source rebuild is not claimed.

For the quickstart or OAI RFSIM example, load the new `monitoring-ml.yaml`
beside its existing `monitoring.yaml`:

```python
config = MonitoringDeployment.from_yaml_file("monitoring-ml.yaml")
```

These ML profiles enable NGAP, PFCP, mapper coverage, path and node metrics.
The existing `monitoring.yaml` profiles remain opt-out. For other deployments,
enable the same `observability` switches in their configuration; image references
default to the selected published builds and can still be overridden explicitly.

In the monolithic OAI RFsim example and its live trial, the orchestrator selects
the stock Open5GS SMF and UPF images. The pinned images above are for monitoring
collectors, not core network functions. Custom core KPIs are off in the
supplied RFsim profiles.

## Live collectors

`ObservabilitySettings` adds four optional collectors and node telemetry:

| Setting | Source | What it measures |
| --- | --- | --- |
| `ngap` | Existing AMF packet sniffer | N2 PDU-session resource setup request/response timing and outcomes; unmatched, expired and untrackable observations |
| `pfcp` | Existing SMF packet sniffer | N4 session-establishment exchanges, Cause outcomes and correlation diagnostics |
| `mapper_coverage` | Existing mapper `/metrics` | Stored context counts, missing mapping fields and bounded Redis collection health |
| `user_plane_paths` | External gNB/UPF ephemeral probe | Interface counters and passive GTP-U observations, path discovery and capture health |
| `node_metrics` | Standard node exporter | Host CPU, memory, pressure, scheduling, networking, disk and clock metrics where supported |

The new protocol metrics are hooks inside the current sniffers, not additional
duplicate sniffers. Mapping remains gNB/session/endpoint scoped. Existing eBPF
latency and per-UE N3 throughput remain separate and are not replaced.
NGAP matching includes the SCTP association; PFCP matching uses the observed
peer pair and sequence. Protocol timers are local request/response observations,
not complete registration/session establishment or end-to-end user-plane latency.
Missing observations do not prove a network fault.

## Image sources and builds

The maintained runtime sources and Dockerfiles belong to the sibling
`nador-images` repository, not this Python deployment library:

```bash
cd ../nador-images
docker build --platform linux/amd64 -f amf-sniffer/Dockerfile -t amf-sniffer:ml-test .
docker build --platform linux/amd64 -f smf-sniffer/Dockerfile -t smf-sniffer:ml-test .
docker build --platform linux/amd64 -f ue-mapper/Dockerfile -t ue-mapper:ml-test .
docker build --platform linux/amd64 -f user-plane-path/Dockerfile -t user-plane-path:ml-test .
```

These commands build locally but do not publish. The initial `2026w45-ml`
builds are published and publicly pullable; prefer the selected commit references
for deployment unless editing image sources.
The old `.local/observability-images` contexts are superseded snapshots, not
maintained sources. Select your published image references explicitly:

```python
from nador import ObservabilitySettings

observability = ObservabilitySettings(
    ngap=True, amf_image="YOUR_REGISTRY/amf-sniffer:YOUR_TAG",
    pfcp=True, smf_image="YOUR_REGISTRY/smf-sniffer:YOUR_TAG",
    mapper_coverage=True, mapper_image="YOUR_REGISTRY/ue-mapper:YOUR_TAG",
    user_plane_paths=True, path_image="YOUR_REGISTRY/user-plane-path:YOUR_TAG",
    node_metrics=True,
)
```

Supply this object as `observability=` in your normal `MonitoringDeployment`.
Use the existing targets for AMF, SMF, gNB and UPF. Collector images are required
when enabled: old images do not acquire these metrics from configuration alone.

Services select each target's existing pod labels; Prometheus discovers individual
endpoints, preserving pod/node identities across replicas. The path controller
does not wait for UEs or traffic before deployment can finish. Failed injection
is reported in its logs and through absent/down metrics; verify those after deploy.

For gNB N3 discovery, `n3_interface` is an explicit override;
`n3_network_names` selects a Multus attachment. Otherwise N3-like interface or
attachment names are provisional hints and observed GTP-U can confirm/discover
the interface. Ambiguous candidates remain unknown. UPF defaults to its existing
packet target interface unless an N3 override/network selection is supplied.
N6 uses the local default route unless `n6_interface` is set; verify this represents
the intended data-network path. Interface counters include all traffic on that
interface; they are not per-UE throughput or end-to-end packet loss.

Path metrics default to port 9403, NGAP to 9102 and PFCP to 9104.
Changes to an active probe/sniffer configuration require removing monitoring
first; it will not silently leave an old image/settings running.

## Experiment artifacts

```python
from nador.artifacts import ObservabilityRun

async with ObservabilityRun(
    "results/baseline-001", monitoring=monitoring,
    collect_setup=True, collect_ue_context=True, collect_loki=True,
    interval_seconds=2, max_seconds=7200,
) as run:
    with run.section("baseline"):
        await your_existing_workload()
```

Outputs retain `ExperimentRun` timelines and metric exports, and add:

- `setup/before` and `setup/after`: read-only host/cluster metadata, image IDs,
  resources and configuration fingerprints, not raw ConfigMaps or Secrets.
- `ue_context/history.jsonl`: bounded sampled inventory, including the current
  gNB, tunnel and PFCP session identities; polls can miss transient changes.
- `ue_context/summary.json` and collection status: errors and truncation hints.
- `logs/`: optional bounded Loki export; a full response page is flagged as
  potentially truncated, not silently treated as complete.

SSH access uses the supplied monitoring manager. Prometheus and Loki forwards
exist only during final export; mapper sampling uses the Kubernetes service proxy
on the control node (mapper service port 8080). Direct URLs are also supported.
Workload exceptions are preserved; collection errors are retained in metadata.
Artifacts contain UE identities and topology: treat them as sensitive research data.

Single-gNB OAI RFsim and the multi-gNB/two-DU OAI trial passed the live
monitoring/ML artifact checks. OCUDU ZeroMQ passed the NGAP, mapping, probe and
traffic stages but its complete artifact export remains unvalidated after
high-volume PHY logs exhausted the export budget. Physical-RU portability
has not yet been validated.

The OAI validator checks native per-radio/per-RNTI DL and UL goodput rather
than requiring the removed calculator `mac_throughput` metric. Goodput is a
gNB-reported rate; it is not relabeled as a cumulative byte counter.

Loki exports split full query pages into disjoint time windows and stream the
accepted pages to disk. The default budget is 1,024 queries; set
`loki_max_queries` on `ObservabilityRun` to adjust it for larger experiments.
The summary records the budget and whether it was exhausted. Incomplete exports
are still flagged as possible truncation and fail artifact validation.
For noisy ZeroMQ PHY logs, an explicit selector can omit routine info messages:

```python
loki_selector = r'{namespace=~".+"} !~ `\[(PHY_LIB|PHY[0-9]*(-NR)?|MAC(-NR)?|RLC(-NR)?|PDCP(-NR)?|GTPU|GW|SCHED)[ ]*\] \[I\]|^[ ]+[0-9a-fA-F]{4}:`'
```

For these ZMQ trials, the selector omits routine packet-layer INFO messages
and packet hex-dump lines. It retains signaling INFO and warning/error messages.
This filters only the exported evidence, not ingestion or retention in Loki.
The selector is recorded in the export summary. Warnings and errors are retained.
The complete filtered ZeroMQ export still requires validation; physical-RU OCUDU
needs its own check of log volume and appropriate filtering.

Read-only availability checks:

```bash
python -m nador.artifacts.check_observability \
  --prometheus-url http://HOST:30095 --loki-url http://HOST:31000 \
  --out results/observability_check.json
```

Empty/NaN query results are unknown or conditional, not healthy zero. Reattach
UEs after sniffers start to validate procedure timing, then run traffic to check
path observations. Validate each deployment before experiments.

Active connectivity probing, controlled-fault datasets, model training and LLM
diagnosis are later work; this port supplies collection, not a trained model.
