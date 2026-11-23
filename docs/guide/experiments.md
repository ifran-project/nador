# Experiments

`ExperimentRun` records the sections of a workload and saves what the deployment observed while
it ran. Nothing needs to be exported by hand afterwards.

```python
from nador.artifacts import ExperimentRun

async with ExperimentRun("results/run1", name="ping", monitoring=monitoring) as run:
    with run.section("baseline"):
        await asyncio.sleep(5)
    with run.section("ping"):
        output = await ue.run_command(command=["ping", "-c", "10", "8.8.8.8"], quiet=True)
    run.save_json("ping.json", output)
```

A section that raises marks the run `failed`, but evidence is still collected; cancelling the
notebook cell marks it `cancelled` and still stops every collector.

## What a run saves

| Path | Content |
| --- | --- |
| `experiment_metadata.json` | name, run id, time zone, status (`completed`, `failed`, `cancelled`), errors |
| `timeline_events.jsonl`, `timeline_summary.*`, `timeline_recap.txt` | each section's start, end and status |
| `prometheus_timeseries.csv`, `prometheus_export_summary.json` | every query of the queries file, at `step` resolution |
| `by_window/` | the same samples split per section |
| `logs/` | Loki logs of the run's time span, when a `logs` metric is on |
| `ue_context/` | UE, IP, slice and tunnels sampled during the run, when the UE mapper is deployed |
| `setup/before/`, `setup/after/` | host and cluster snapshots |
| `csi/<component>/` | the CSI file cut per section, with `collect_csi=True` and the `csi` metric on ([CSI](csi.md)) |
| anything you `save_json` | your own results, such as iperf reports |

If a collector fails, the run records it under `collection_errors`, finishes the others, and raises
at the end so a failure is never silent.

## Options

| Option | Default | What it does |
| --- | --- | --- |
| `name` | "experiment" | Name recorded in `experiment_metadata.json` |
| `monitoring` | none | The monitoring manager: everything it deployed is saved, through SSH tunnels |
| `prometheus_url` | none | A directly reachable Prometheus instead of `monitoring` (metrics only) |
| `queries_file` | NADOR's `queries.json` | The Prometheus queries to export |
| `step` | "1s" | Resolution of the exported time series |
| `timezone` | "UTC" | Time zone of the readable timestamps in the timeline and its recap |
| `collectors` | () | Your own collectors, run alongside the built-in ones |
| `collect_setup` | True | Snapshot the hosts and cluster before and after |
| `setup_hosts` | every reachable node | Only snapshot these nodes |
| `collect_ue_context` | True | Sample the UE context during the run |
| `collect_csi` | False | Cut the CSI file of every logging gNB/DU by section into `csi/<component>/` (needs the `csi` metric) |
| `mapper_url` | deployed mapper | A UE mapper reachable directly, instead of through the cluster |
| `namespace`, `service`, `service_port` | monitoring namespace, "ue-mapper-api", 8080 | Where the UE mapper runs |
| `interval_seconds` | 2 | Seconds between UE context samples |
| `timeout_seconds` | 5 | Timeout of one UE context request |
| `max_seconds` | 7200 | Longest run the UE sampler follows |
| `limit` | 5000 | Most UEs per sample |
| `loki_url` | deployed Loki | A Loki reachable directly; also turns on log export without `monitoring` |
| `loki_selector` | everything collected | Which logs to export (a LogQL selector such as `{namespace="core"}`) |
| `loki_limit` | 5000 | Most log lines per query |
| `loki_max_queries` | 1024 | Most Loki queries per run |

## Your own collectors

Anything with `start`, `stop` and `collect` steps can run alongside the built-in collectors (see
`nador.artifacts.Collector`). A collector saves files with `run.save_json(path, data)` and records
a problem without raising with `run.error(label, problem)`.

```python
from nador.artifacts import Collector


class UeAddresses(Collector):
    async def start(self, run):
        run.save_json("ue_addresses/before.json", await read_addresses())

    async def stop(self, run):
        run.save_json("ue_addresses/after.json", await read_addresses())


async with ExperimentRun("results/run2", monitoring=monitoring, collectors=[UeAddresses()]) as run:
    ...
```

## Traffic with iperf3

`nador.traffic.Iperf` runs iperf3 between UEs and a server node. `setup` installs iperf3 on the
server and on the UEs that lack it, and finds each UE's address on its tunnel interface. It works
with UE containers (`UEManagement`) and with physical UEs reached over SSH
(`ProvisioningManagement`).

```python
from nador.traffic import Iperf

iperf = await Iperf.setup(ue, server="node1", interface="oaitun_ue1", clients=["sim_ue"])
async with ExperimentRun("results/run3", monitoring=monitoring) as run:
    for direction in ("downlink", "uplink"):
        with run.section(direction):
            report = await iperf.run("sim_ue", direction, 20)
        run.save_json(f"{direction}.json", report)
```

`iperf.run` starts a one-shot server on a fresh port and runs the UE side; `iperf.serve(port)` and
`iperf.client(ue, direction, seconds, port)` do the two halves separately, for several UEs at once.
Each returns iperf3's JSON report; a failed test returns the report with iperf's `error`, so it can
be saved before acting on it. `nador.artifacts.iperf_summary("results/run3")` prints one line per
saved report: sent and received throughput and retransmissions, or the error.

## Checking that metrics arrive

`nador-check-metrics` lists scrape targets that are down, queries that return no data, and whether
Loki receives logs. It writes a JSON report and exits non-zero when something is wrong:

```bash
nador-check-metrics --prometheus-url http://127.0.0.1:30095 --loki-url http://127.0.0.1:31000 --out report.json
```
