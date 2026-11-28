# The metric registry

A NADOR deployment exposes more than 2000 metrics: about 500 from the RAN, the core and NADOR's probes, and the rest
from the platform (nodes, containers, Kubernetes, Prometheus, Loki). The registry,
`src/nador/assets/metric_registry.yaml`, says what each one means and how to read it.

```python
from nador.metric_registry import load_registry

registry = load_registry()
metric = registry.describe("ue_dl_nof_nok")
metric.meaning     # 'Downlink transmissions not acknowledged (HARQ NACK) in the period; BLER = nok / (ok + nok)'
metric.better      # 'lower'
metric.ue_labels   # ['rnti', 'pci', 'cell_index']
metric.produced_by # 'ran.ran_kpis': the profile metric that collects it
```

## What it holds

| Section | What | One entry per |
| --- | --- | --- |
| `metrics` | The 5G and probe metrics: OAI and OCUDU RAN KPIs, Open5GS's own metrics, NADOR's sniffers and user-plane, host and quality probes, and what NADOR computes (KPI calculator, RNTI mapper, UE mapper) | Metric, under the name to query |
| `families` | The platform's metrics | Name prefix (`node_`, `container_`, `kube_`, `prometheus_`...) |
| `queries` | NADOR's named queries (`queries.json`), with the metrics they read and the labels they group by, and why a query returns nothing when it does | Query |
| `ue_identity` | How a per-UE series is tied to a UE | Layer (RAN, user plane, sessions) |

Each metric entry gives:
- **What it is:** its kind (`exporter`, `probe` or `computed`), its layer and component, its Prometheus type and unit,
  and what it means.
- **How to read it:** which way is good (`better`), the labels that identify a UE (`ue_labels`), and how to
  aggregate it (`use`).
- **Where it comes from:** its source, the profile metric that collects it (`produced_by`), and the implementations
  that export it.
- **How sure the entry is:** where its description comes from (`evidence`), and whether a person checked it
  (`verified`).

`describe` also takes a series name: `x_bucket`, `x_sum` and `x_count` give the histogram `x`, and a platform metric
gives its family.

## Per-UE series

- **User-plane probes:** their series already carry the UE's `imsi`, `ue_ip` and `slice`, from the UE mapper.
- **RAN KPIs:** these are per RNTI, which is unique only within a cell. Name the UE with the RNTI mapper's
  `ran_ue_info`:

  ```promql
  ue_dl_brate * on(ran_component, pci, rnti) group_left(ue_name, imsi) ran_ue_info
  ```

  OAI series name their component in `service` (`metrics-ran-<component>`): `label_replace` it into `ran_component`
  first, as the dashboards do.

## Reading OCUDU's metrics

OCUDU reports each field once per reporting period, and NADOR's collector exports it as a gauge. A count such as
`ue_dl_nof_ok` is therefore the count of that period, not a running total:
- read it as it is, or as a ratio (BLER = nok / (ok + nok));
- never use `rate()` on it.

## How sure an entry is

Each metric entry was checked against the code that produces it, for the versions IFRAN deploys:
- NADOR's probes: nador-images, at the commit `images.json` pins (and its `ml-extended` branch for the host
  probes);
- OAI 2026.w42: the stats files the OAI metrics parser reads;
- OCUDU release_26_04: its JSON metrics;
- Open5GS 2.8.0: its network functions' own metrics.

The entry cites that code first in its `evidence` (`source: ...`) and is marked `verified: true`. Which way is
good (`better`) is a judgment, not read from code.

`caveats` say where a metric is wrong, empty or easy to misread. For example:
- `mac_throughput` stays empty with OAI 2026.w42 to 2026.w47, which print no per-LCID byte counts;
- OAI's HARQ rounds, errors and DTX counts are running totals since the UE connected, read with `increase()`;
- with several cells in one OAI pod, its L1 metrics are not per cell.

A query that returns nothing says why in its `problems`. `registry.unverified()` lists what no one has checked,
such as the named queries.
