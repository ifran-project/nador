# NADOR

**Network Analytics, Diagnostics, Observability, and Reporting for Mobile Networks.**

Named after Nador, a city in northeastern Morocco associated with the idea of a
lookout, NADOR helps teams monitor mobile network performance, investigate issues,
and report their findings.

NADOR extends [IFRAN](https://github.com/ifran-project/ifran), the Interoperable
Framework for Reproducible Automation of Networks. IFRAN handles network
provisioning and slice management; NADOR adds monitoring and experiment collection.

It integrates Prometheus, Grafana and Loki, correlates UE/slice/tunnel/session
information, deploys latency, throughput and host probes, and exports experiment
timelines and metrics. Network deployment remains handled by upstream managers.

Install from GitHub. NADOR installs the supported IFRAN release from PyPI:

```bash
python -m pip install --upgrade "git+https://github.com/ifran-project/nador.git@main"
```

```python
from nador import MonitoringManagement, MonitoringDeployment
```

Start with the [minimal tutorial](examples/quickstart/tutorial.ipynb): installation,
imports, monitoring deployment and one experiment with artifact collection.

Every container image NADOR deploys is pinned in [`assets/images.json`](src/nador/assets/images.json).
A profile's `images:` section overrides individual entries by key. After merging an
image update, reinstall Nador, restart the notebook kernel, and redeploy.

## Development

```bash
python -m pip install -e ".[dev]"
ruff check src tests && ruff format --check src tests
pyright
pytest
```

The tests plan every example profile offline and compare the rendered Helm
values with `tests/snapshots/`. After an intended change, regenerate them with
`NADOR_UPDATE_SNAPSHOTS=1 pytest` and review the diff. The settings the controller
image reads are described in [docs/controller-contract.md](docs/controller-contract.md).

## Monitoring

Deploy monitoring after the RAN and before UE attachment, so the sniffers
observe session establishment. Use a topology-specific profile:

```python
config = MonitoringDeployment.from_yaml_file("monitoring.yaml")
monitoring = MonitoringManagement(preceding=ran, config=config)
await monitoring.deploy()
```

### Metrics

A profile lists every metric under `metrics:`. `true` turns a metric on, `false`
leaves it out, and a mapping turns it on with options:

```yaml
metrics:
  rtt: {packet_pairing: true, imsis: ["001010000000001"]}  # round-trip latency per UE/slice
  throughput: true             # N3 throughput per UE/slice
  n3_one_way: false            # one-way gNB-UPF latency
  upf_transit: false           # one-way latency through the UPF; needs {peer, test_port}
  user_plane_paths: false      # per-path packet counters on gNBs and UPFs
  ngap: false                  # session setup times from the AMF sniffer
  pfcp: false                  # session establishment times from the SMF sniffer
  ue_mapper_coverage: false    # how much UE context the mapper resolved
  core_kpis: false             # Open5GS KPIs
  ran_kpis: false              # OAI or OCUDU KPIs
  logs: true                   # Loki and Promtail
  node_metrics: false          # node exporter
  scheduler_wait: false        # host probes: CPU, drops, queues, sockets, TCP, F1/E1, quality
  kernel_drops: false
  attributed_drops: false
  network_queues: false
  socket_counters: false
  tcp_flows: false             # needs {identity_secret: <Kubernetes Secret>}
  split_signaling: false
  measurement_quality: false
```

Probes the controller manages accept `{on_demand: true}`: they are deployed
stopped, and `nador-controller start <probe>` starts them later. Rarely changed
knobs (probe intervals, sniffer timeouts) live under `tuning:`. Every profile must
list all metrics, so it always says what it collects.

For OCUDU native KPIs, turn on `ran_kpis` and call
`nador.configure_ran_kpis(ran, config)` **before** deploying the RAN.

### Experiments

`ExperimentRun` records each `section` of a workload and saves everything the
deployment observed meanwhile: metrics split per section, logs when `logs` is on,
the UE context over time when the UE mapper is deployed, and snapshots of the
hosts and cluster before and after.

```python
async with ExperimentRun("results/run1", monitoring=monitoring) as run:
    with run.section("ping"):
        ...
```

For throughput, `nador.traffic.Iperf` installs iperf3 on a server node and on the
UEs that lack it, then runs tests from the UEs (containers or physical UEs):

```python
iperf = await Iperf.setup(ue, server="node4", interface="oaitun_ue1", clients=["sim_ue"])
async with ExperimentRun("results/run2", monitoring=monitoring) as run:
    with run.section("downlink"):
        run.save_json("downlink.json", await iperf.run("sim_ue", "downlink", 20))
```

Other collectors can run alongside: anything with `start`, `stop` and `collect`
steps (see `nador.artifacts.Collector`) can be passed as
`ExperimentRun(..., collectors=[...])`. To check that the monitoring stack
actually produces data, run `nador-check-metrics --prometheus-url <url> --out report.json`.

Save both deployment models with `monitoring.output()`. After restarting a
kernel, use `MonitoringManagement.from_yaml_files(metamodel_file, deployment_model_file)`
to restore the handle without redeploying. Keep the saved models private.

Other examples are under [examples/monitoring](examples/monitoring):

- [RFsim](examples/monitoring/rfsim): monolithic and split OAI radios.
  The monolithic OAI RFsim example uses stock Open5GS SMF and UPF images
  selected by IFRAN and leaves custom core KPIs off.
- [Multi-gNB](examples/monitoring/multi-gnb-2du/test.ipynb): 20 UEs, a monolithic
  gNB plus a CU/two DUs, three slices and two UPFs; includes on-demand traffic.
- [OCUDU ZMQ](examples/monitoring/ocudu-zmq) and [R2lab](examples/monitoring/r2lab):
  additional deployment configurations.

Copyright (c) 2026 Yassir Amami. The extension is licensed under the
[BSD 3-Clause Clear License](LICENSE). Reused/adapted upstream example and chart
material retains the [IFRAN license notice](LICENSES/IFRAN.txt).
External dependencies and container images retain their own licenses.
