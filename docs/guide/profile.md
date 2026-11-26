# The monitoring profile

A profile (`monitoring.yaml`) says what to collect and from which network functions. It is read
with `MonitoringDeployment.from_yaml_file(path)`, which checks everything before any cluster work;
`nador.config.validate_deployment(data)` checks the same rules on a dictionary.

```python
from nador import MonitoringDeployment, MonitoringManagement

config = MonitoringDeployment.from_yaml_file("monitoring.yaml")
monitoring = MonitoringManagement(preceding=ran, config=config)
```

## Layout

```yaml
namespace: monitoring                    # where NADOR's releases go
kubeconfig: /etc/kubernetes/admin.conf   # on the Kubernetes master
metrics: {...}                           # what to collect, by group (below)
targets: [...]                           # the network functions to watch (below)
images: {}                               # overrides of assets/images.json, by key
tuning: {}                               # probe knobs; the defaults suit most deployments
```

Other settings, with their defaults: `node_name` (none: any node), `prometheus_scrape_interval`
(`1s`), `prometheus_retention_period` (`5d`), `operation_timeout_seconds` (300), and the node ports
`prometheus_node_port` (30095), `grafana_node_port` (32005) and `loki_node_port` (31000).

## Metrics

Every metric is listed, under the group of where it measures. `false` leaves a metric out. A
metric without options is turned on with `true`; a metric with options is turned on by writing
**all** of them, defaults included, so the profile always shows exactly what runs.

```yaml
metrics:
  ran:
    ran_kpis: false
    csi: false
    split_signaling: false
    logs: true
  core:
    ngap: false
    pfcp: false
    throughput: {on_demand: false}
    upf_transit: false
    ue_mapper_coverage: false
    logs: true
  e2e:
    rtt: {on_demand: false, at: [gnb, upf], packet_pairing: false, imsis: [], slices: []}
    n3_one_way: false
    user_plane_paths: false
  host:
    node_metrics: false
    logs: true
    scheduler_wait: false
    kernel_drops: false
    attributed_drops: false
    network_queues: false
    socket_counters: false
    tcp_flows: false
    measurement_quality: false
```

| Group | Metric | What it collects | Options (default) |
| --- | --- | --- | --- |
| `ran` | `ran_kpis` | OAI or OCUDU RAN KPIs | `ocudu_port` (8001) |
| `ran` | `csi` | Uplink channel (SRS) per UE, RB, antenna and port, logged by the OAI or OCUDU gNB/DU ([CSI](csi.md)) | `components` (all), `srs_period_ms` (40, OCUDU), `granularity` (`rb`), `subcarrier_sampling` (1), `antennas` (`all`), `ports` (`all`), `output_dir` (per RAN) |
| `ran` | `split_signaling` | F1/E1 signalling between CU and DU (split RANs only) | `on_demand`, `targets` |
| `ran` | `logs` | Logs of the RAN's pods, in Loki | none |
| `core` | `ngap` | Session-setup times, from the AMF sniffer | `port` (9102) |
| `core` | `pfcp` | Session-establishment times, from the SMF sniffer | `port` (9104) |
| `core` | `throughput` | N3 throughput per UE and slice, at the UPFs | `on_demand` |
| `core` | `upf_transit` | One-way latency through the UPF towards a test peer | `on_demand`, `peer`, `test_port` (required) |
| `core` | `ue_mapper_coverage` | How many UEs the mapper resolved to IMSI, IP and tunnels | none |
| `core` | `logs` | Logs of the core's pods (sniffers included), in Loki | none |
| `e2e` | `rtt` | TCP round-trip time per UE and slice, on N3 | `on_demand`, `at` ([gnb, upf]), `packet_pairing` (false), `imsis`, `slices` ([] = all) |
| `e2e` | `n3_one_way` | One-way gNB-UPF latency on N3 | `on_demand`, `log_samples` (false) |
| `e2e` | `user_plane_paths` | Packet counters per user-plane path on gNBs and UPFs | `on_demand`, `port` (9403), `interval_seconds` (5) |
| `host` | `node_metrics` | Node CPU, memory, disk and network (node exporter) | none |
| `host` | `logs` | Logs of NADOR's monitoring pods, in Loki | none |
| `host` | `scheduler_wait` | How long tasks wait for a CPU on the nodes | `on_demand`, `targets` ([] = all) |
| `host` | `kernel_drops`, `attributed_drops` | Packets the kernel drops, and the flows they belonged to | `on_demand`, `targets` |
| `host` | `network_queues` | Queueing in network interface queues | `on_demand`, `targets` |
| `host` | `socket_counters`, `tcp_flows` | Socket statistics, and per-flow TCP evidence | `on_demand`, `targets`; `tcp_flows` also `identity_secret` |
| `host` | `measurement_quality` | Freshness and availability of chosen Prometheus queries | `on_demand`, `targets`, `queries` ([]) |

Notes:

- **`rtt` needs traffic to measure**: it times TCP data against its ACK (iperf, HTTP, SSH) and a
  ping request against its reply; samples carry `kind="tcp_ack"` or `kind="icmp_echo"`. `at`
  chooses the measuring points; `packet_pairing` matches the same packet at both to split the
  latency into segments, so it needs `at: [gnb, upf]` and `on_demand: false`.
- **`on_demand: true`** deploys a probe stopped; start it later with `monitoring.start(...)`
  (see [Controlling probes](probes.md)).
- **Host probes that share a row run in one probe**, so they start and stop together.
  `attributed_drops` and `tcp_flows` only collect when deployed running.
- **`ran_kpis` and `csi` change the RAN**: call `nador.configure_ran_monitoring(ran, config)` before `ran.deploy()`.
  It makes an OCUDU gNB publish its metrics (`ran_kpis`) and sets up the CSI logger (`csi`); with both off it
  changes nothing. It also refuses OCUDU log settings that would hide what NADOR reads from the RAN's logs
  (see [Logs](logs.md)). The CSI logger is part of the gNB image, so turning it on or off means deploying the RAN
  again. A run collects it with
  `ExperimentRun(..., collect_csi=True)` (see [CSI](csi.md)).
- **`ran_kpis` and `csi`** also deploy the RNTI mapper: it names the UE behind each RNTI in the dashboard
  panels, from the RRC of the OAI or OCUDU gNB, CU or CU-CP and the AMF. It reads the AMF log of an OAI, Open5GS
  or free5gc core, and prefers the UE mapper's inventory (deployed with a session metric) when there is one.
  With an OCUDU CU-CP and several DUs, it names the DU of each UE from the gNB-DU ID in the F1AP lines of the
  CU-CP: the `gnb_du_id` of the DU's `advanced_config` when it has one, else the `gnbDuId` of the meta model
  (see [Logs](logs.md)). The dashboard's RAN component selector then shows each DU's UEs by name.

## Targets

Each target binds monitoring to one deployed network function:

```yaml
targets:
  - {category: core, component: amf, role: amf, container: amf, interface: eth0, metrics_port: 9090}
  - {category: core, component: smf, role: smf, container: smf, interface: eth0, metrics_port: 9090}
  - {category: core, component: upf, role: upf, container: upf, interface: eth0, metrics_port: 9090}
  - {category: ran, component: gnb, role: gnb, container: gnb, interface: eth0, metrics_port: 9090}
```

| Field | Meaning |
| --- | --- |
| `category` | `core`, `ran` or `ue`: which IFRAN manager owns the component |
| `component` | its name in the deployment model |
| `role` | `amf`, `smf`, `gnb` or `upf` when packets are captured there (sniffers, N3 probes) |
| `container`, `interface` | where to capture; required with a `role` |
| `selector` | pod labels, when the default release labels do not match |
| `metrics_port`, `metrics_path` | scrape the component's own metrics (default path `/metrics`) |

Several gNB or UPF targets (a CU and a gNB, two UPFs) are supported: probes run on each.

The example profiles watch an Open5GS core, whose network functions export Prometheus metrics on 9090. OAI's
core network functions export none: leave `metrics_port` out of their targets, or Prometheus reports them down.
(IFRAN's OAI core has no BSF, so its meta model lists the core components without one.)

## Tuning

`probe_interval_seconds` (5), `probe_timeout_seconds` (2), `probe_capture_limit` (8),
`packet_run_id` ("experiment"), `packet_state_limit` (4096), `packet_ttl_seconds` (30),
`sniffer_pending_timeout_seconds` (30), `one_way_sample` (16).

## Errors

Every problem in a profile is reported at once, by its place in the file:

```text
Invalid monitoring profile monitoring.yaml:
  - metrics.e2e.rtt: list every option, e.g. rtt: {on_demand: false, at: [gnb, upf], packet_pairing: false, imsis: [], slices: []}
  - metrics.core.rtt; it belongs under metrics.e2e.rtt
```

Only `true` and `false` are booleans (`yes`/`no`/`on`/`off` are refused), duplicate keys are
refused, and images must be pinned by digest.
