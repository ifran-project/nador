<!-- docs-start -->
# NADOR

<p align="center">
  <a href="https://pypi.org/project/nador/">
    <img src="https://img.shields.io/pypi/v/nador" alt="Latest PyPI version" />
  </a>
  <a href="https://ifran-project.github.io/nador/">
    <img src="https://img.shields.io/badge/docs-GitHub%20Pages-blue?logo=github" alt="Documentation" />
  </a>
  <a href="https://github.com/ifran-project/nador/actions/workflows/ci.yml">
    <img src="https://github.com/ifran-project/nador/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI status" />
  </a>
  <a href="https://sonarcloud.io/summary/new_code?id=ifran-project_nador">
    <img src="https://sonarcloud.io/api/project_badges/measure?project=ifran-project_nador&metric=alert_status" alt="Quality gate status" />
  </a>
  <a href="https://sonarcloud.io/component_measures?id=ifran-project_nador&metric=coverage">
    <img src="https://sonarcloud.io/api/project_badges/measure?project=ifran-project_nador&metric=coverage" alt="Test coverage" />
  </a>
  <a href="https://github.com/ifran-project/nador/blob/main/LICENSE">
    <img src="https://img.shields.io/badge/License-BSD--3--Clause--Clear-blue.svg" alt="BSD 3-Clause Clear license" />
  </a>
  <a href="https://github.com/ifran-project/nador/releases">
    <img src="https://img.shields.io/github/v/release/ifran-project/nador?color=orange" alt="Latest release" />
  </a>
  <a href="https://github.com/ifran-project/nador/blob/main/CONTRIBUTING.md">
    <img src="https://img.shields.io/badge/PRs-Welcome-brightgreen" alt="Pull requests welcome" />
  </a>
</p>

**Network Analytics, Diagnostics, Observability, and Reporting for Mobile Networks.**

Named after Nador, a city in northeastern Morocco associated with the idea of an observatory, NADOR helps teams monitor mobile network performance, investigate issues,
and report their findings.

NADOR extends [IFRAN](https://github.com/ifran-project/ifran), the Interoperable
Framework for Reproducible Automation of Networks. IFRAN handles network
provisioning and slice management; NADOR adds monitoring and experiment collection.

It integrates Prometheus, Grafana and Loki, correlates UE/slice/tunnel/session
information, deploys latency, throughput and host probes, and exports experiment
timelines and metrics. Network deployment remains handled by upstream managers.

Install the latest release from PyPI; it brings the supported IFRAN release with it:

```bash
python -m pip install --upgrade nador
```

To try changes that are not released yet, install the `develop` branch instead:

```bash
python -m pip install --upgrade "git+https://github.com/ifran-project/nador.git@develop"
```

```python
from nador import MonitoringManagement, MonitoringDeployment
```
<!-- docs-end -->

Documentation: <https://ifran-project.github.io/nador/> (guides, the quickstart, every example and
the API reference).

Start with the [minimal tutorial](examples/quickstart/tutorial.ipynb): installation,
imports, monitoring deployment and one experiment with artifact collection.

Every container image NADOR deploys is pinned in [`assets/images.json`](src/nador/assets/images.json).
A profile's `images:` section overrides individual entries by key. After merging an
image update, reinstall Nador, restart the notebook kernel, and redeploy.

## Contributing

Contributions are welcome: see
[CONTRIBUTING.md](https://github.com/ifran-project/nador/blob/main/CONTRIBUTING.md) for how
to set up, run the checks and open a pull request.

## Monitoring

Deploy monitoring after the RAN and before UE attachment, so the sniffers
observe session establishment. Use a topology-specific profile:

```python
config = MonitoringDeployment.from_yaml_file("monitoring.yaml")
monitoring = MonitoringManagement(preceding=ran, config=config)
await monitoring.deploy()
```

### Metrics

A profile lists every metric under `metrics:`, grouped by where it measures: the RAN,
the core, end to end between them, or the hosts. `false` leaves a metric out. A metric
without options is turned on with `true`; a metric with options is turned on by writing
all of them, defaults included, so the profile shows exactly what runs. If one is missing
or in the wrong group, the error message says what to write. `logs` collects pod logs into
Loki per group: the RAN's pods, the core's pods, and NADOR's own monitoring pods under `host`.

```yaml
metrics:
  ran:
    ran_kpis: false                 # OAI or OCUDU KPIs: {ocudu_port}
    split_signaling: false          # F1/E1 signalling (split RANs): {on_demand, targets}
    logs: true                      # logs of the RAN's pods
  core:
    ngap: false                     # session setup times from the AMF sniffer: {port}
    pfcp: false                     # session establishment times from the SMF sniffer: {port}
    throughput: {on_demand: false}  # N3 throughput per UE/slice, at the UPFs
    upf_transit: false              # one-way latency through the UPF: {on_demand, peer, test_port}
    ue_mapper_coverage: false       # how much UE context the mapper resolved
    logs: true                      # logs of the core's pods
  e2e:
    # TCP round-trip time per UE and slice, at the gNB, the UPF or both. packet_pairing matches
    # the same packet at both to split the path; empty imsis/slices mean every UE and slice.
    rtt: {on_demand: false, at: [gnb, upf], packet_pairing: false, imsis: [], slices: []}
    n3_one_way: false               # one-way gNB-UPF latency: {on_demand, log_samples}
    user_plane_paths: false         # per-path packet counters: {on_demand, port, interval_seconds}
  host:
    node_metrics: false             # node exporter
    logs: true                      # logs of NADOR's monitoring pods
    scheduler_wait: false           # host probes: {on_demand, targets}
    kernel_drops: false
    attributed_drops: false
    network_queues: false
    socket_counters: false
    tcp_flows: false                # also needs identity_secret: <Kubernetes Secret>
    measurement_quality: false      # also takes queries
```

Metrics the controller manages (`rtt`, `throughput`, `n3_one_way`, `upf_transit`,
`user_plane_paths`, `split_signaling` and the host probes) take `on_demand: true` to deploy stopped.
On a running deployment they start, stop and change scope without redeploying:

```python
await monitoring.start("rtt", slices=["01:ffffff"], at=["upf"])  # one slice, at the UPF only
await monitoring.start("rtt", slices=[], at=["gnb", "upf"])      # back to every UE, both anchors
await monitoring.stop("rtt")
```

Any metric can also be deployed or undeployed on a running deployment. Options are
written as in the profile; only the releases the change affects are installed, upgraded
or removed, and the AMF/SMF sniffers start with the first metric that needs them:

```python
await monitoring.deploy_probe("scheduler_wait", {"on_demand": False, "targets": []})
await monitoring.remove_probe("scheduler_wait")
```

`stop` keeps a probe installed and only removes its running pods, so `start` brings it back in
seconds; `remove_probe` uninstalls it.

Rarely changed knobs (probe intervals, sniffer timeouts) live under `tuning:`.

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

Copyright (c) 2026 Yassir Amami and the NADOR contributors. The extension is licensed under the
[BSD 3-Clause Clear License](LICENSE). Reused/adapted upstream example and chart
material retains the [IFRAN license notice](LICENSES/IFRAN.txt).
External dependencies and container images retain their own licenses.
