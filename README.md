# NADOR

**Network Analytics, Diagnostics, Observability, and Reporting for Mobile Networks.**

Named after Nador, a city in northeastern Morocco associated with the idea of a
lookout, NADOR helps teams monitor mobile network performance, investigate issues,
and report their findings.

NADOR extends [IFRAN](https://github.com/ifran-project/ifran), the Infrastructure
Framework for Reproducible Automation of Networks. Inspired by Ifrane and an
anagram of INFRA, IFRAN handles reproducible network provisioning and slice management.
Its current Python distribution and imports remain named `orchestrator`.

It integrates Prometheus, Grafana and Loki, correlates UE/slice/tunnel/session
information, deploys latency and N3 throughput probes, and exports experiment
timelines and metrics. Network deployment remains handled by upstream managers.

Install from GitHub:

```bash
python -m pip install --upgrade "git+https://github.com/ifran-project/nador.git@ml-observability" --extra-index-url https://gitlab.inria.fr/api/v4/projects/70914/packages/pypi/simple
```

```python
from nador import MonitoringManagement, MonitoringDeployment
```

Start with the [minimal tutorial](examples/quickstart/tutorial.ipynb): installation,
imports, monitoring deployment and one experiment with artifact collection.

Default monitoring image versions live in [`assets/images.json`](src/nador/assets/images.json).
Image builds can propose updates through a pull request; deployments change after you merge it.
Explicit scenario overrides still take precedence.

## Monitoring

Deploy monitoring after the RAN and before UE attachment, so the sniffers
observe session establishment. Use a topology-specific profile:

```python
config = MonitoringDeployment.from_yaml_file("monitoring.yaml")
monitoring = MonitoringManagement(preceding=ran, config=config)
await monitoring.deploy()
```

For OCUDU native KPIs, set `ran_kpis: true` and call
`nador.configure_ran_kpis(ran, config)` **before** deploying the RAN. This does
not enable ML collectors. Use `monitoring-ml.yaml` to enable the optional
NGAP, PFCP, UE-mapper, path, and infrastructure collectors. Use
`nador.artifacts.ObservabilityRun` to export metrics, sampled UE context, and
setup snapshots; Loki export is optional.

Save both deployment models with `monitoring.output()`. After restarting a
kernel, use `MonitoringManagement.from_yaml_files(metamodel_file, deployment_model_file)`
to restore the handle without redeploying. Keep the saved models private.

Other examples are under [examples/monitoring](examples/monitoring):

- [RFsim](examples/monitoring/rfsim): monolithic and split OAI radios.
  The monolithic OAI RFsim example uses stock Open5GS SMF and UPF images
  selected by the orchestrator. Its ML profile uses separate collector
  images and leaves custom core KPIs off.
- [Multi-gNB](examples/monitoring/multi-gnb-2du/test.ipynb): 20 UEs, a monolithic
  gNB plus a CU/two DUs, three slices and two UPFs; includes on-demand traffic.
- [Open5GS throughput profile](examples/monitoring/throughput/monitoring.yaml) and
  [OAI throughput profile](examples/monitoring/throughput-oai/monitoring.yaml):
  configurations for the N3 throughput probe.
- [OCUDU ZMQ](examples/monitoring/ocudu-zmq), including a
  [single-gNB ML profile](examples/monitoring/ocudu-zmq/zmq/monitoring-ml.yaml),
  [B210](examples/monitoring/b210)
  and [R2lab](examples/monitoring/r2lab): additional deployment configurations.

## On-demand probes

Set `latency: true` to enable external RTT. The controller supports one gNB
and one UPF target; AMF and SMF sniffers provide UE mapping. Same-packet
pairing is not supported. If upgrading from injected RTT, recreate the gNB
and UPF pods to clear the old probes before enabling external RTT.

Configure [dedicated probes](examples/quickstart/dedicated-probes.yaml) in your
monitoring profile. The controller can start and stop configured probes without
redeploying the network. Its HTTP API listens on loopback by default:

```bash
kubectl -n monitoring port-forward deployment/monitoring-controller 8787:8787
```

In another terminal:

```bash
nador-controller list
nador-controller start throughput --slice 01:ffffff
nador-controller start rtt --imsi 001010000000100
nador-controller stop rtt
```

RTT and throughput support IMSI and slice filters. When both are set, both
must match. Omitted filters keep their previous values; use `--all` to clear
them. Path probes report interface-wide counters. For one-way probes, set
`one_way.image` and the target interfaces. UPF transit also requires
`one_way.upf_transit.peer` and `one_way.test_port`, the traffic service port.

Check pod readiness or Prometheus after changing a probe: API responses
confirm the requested state, not workload readiness. A Helm upgrade reapplies
the YAML settings. To expose the API remotely, configure `probe_api_tls_secret`
and `probe_api_token_secret` and restrict network access. The CLI uses
`NADOR_CONTROLLER_URL` and `NADOR_CONTROLLER_TOKEN` for remote HTTPS.

Copyright (c) 2026 Yassir Amami. The extension is licensed under the
[BSD 3-Clause Clear License](LICENSE). Reused/adapted upstream example and chart
material retains the [SLICES orchestrator notice](LICENSES/SLICES-orchestrator.txt).
External dependencies and container images retain their own licenses.
