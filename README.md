# NADOR

**Network Analytics, Diagnostics, Observability, and Reporting for Mobile Networks.**

Named after Nador, a city in northeastern Morocco associated with the idea of a
lookout, NADOR helps teams monitor mobile network performance, investigate issues,
and report their findings.

NADOR extends [IFRAN](https://github.com/ifran-project/ifran), the Interoperable
Framework for Reproducible Automation of Networks. IFRAN handles network
provisioning and slice management; NADOR adds monitoring and experiment collection.

It integrates Prometheus, Grafana and Loki, correlates UE/slice/tunnel/session
information, deploys latency and N3 throughput probes, and exports experiment
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

Default monitoring image versions live in [`assets/images.json`](src/nador/assets/images.json).
The notebook profiles inherit these defaults, including the AMF/SMF sniffers
and UE mapper in ML mode. After merging an image update, reinstall Nador,
restart the notebook kernel, and redeploy. Explicit overrides still take precedence.

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

When upgrading a deployment that used the old standalone `one-way` or bundled
`extended-probes` releases, uninstall those releases from the monitoring
namespace before redeploying. In custom profiles, replace bundled
`extended.node_image`/`extended.service_image` with `extended.probe_images`
and remove `observability.path_image`. Path probes now inherit the controller's
external path image unless `external_path_image` is explicitly set.
If gNB or UPF pods still carry the old injected path probes, recreate those
pods during a maintenance window to remove the ephemeral containers.

Multi-UPF throughput still uses its existing deployment by default. To try the
external placement on a fresh deployment, set `probe_controller_image` to a
published controller build with multi-UPF support and keep `throughput: true`.
The same build supports multiple targets for path counters and one-way
latency. Set `one_way.image` to the matching one-way probe build. N3 latency
needs gNB and UPF targets; UPF transit needs no gNB target but still needs
the configured traffic peer and port. RTT retains its single-anchor-per-role limit.

Save both deployment models with `monitoring.output()`. After restarting a
kernel, use `MonitoringManagement.from_yaml_files(metamodel_file, deployment_model_file)`
to restore the handle without redeploying. Keep the saved models private.

Other examples are under [examples/monitoring](examples/monitoring):

- [RFsim](examples/monitoring/rfsim): monolithic and split OAI radios.
  The monolithic OAI RFsim example uses stock Open5GS SMF and UPF images
  selected by IFRAN. Its ML profile uses separate collector
  images and leaves custom core KPIs off.
- [Multi-gNB](examples/monitoring/multi-gnb-2du/test.ipynb): 20 UEs, a monolithic
  gNB plus a CU/two DUs, three slices and two UPFs; includes on-demand traffic.
- [OCUDU ZMQ](examples/monitoring/ocudu-zmq), including a
  [single-gNB ML profile](examples/monitoring/ocudu-zmq/zmq/monitoring-ml.yaml),
  and [R2lab](examples/monitoring/r2lab): additional deployment configurations.

Copyright (c) 2026 Yassir Amami. The extension is licensed under the
[BSD 3-Clause Clear License](LICENSE). Reused/adapted upstream example and chart
material retains the [IFRAN license notice](LICENSES/IFRAN.txt).
External dependencies and container images retain their own licenses.
