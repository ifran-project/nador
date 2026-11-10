# Monitoring

Follow the [minimal tutorial](examples/quickstart/tutorial.ipynb) for installation,
configuration, deployment and artifact collection in one notebook.

Install the latest merged library from GitHub `main` in your notebook:

```python
%pip install --upgrade "git+https://github.com/yassir63/nador.git@main" --extra-index-url https://gitlab.inria.fr/api/v4/projects/70914/packages/pypi/simple
```

Restart the kernel after installation, then run your core/RAN setup before
creating the monitoring manager below.

The [multi-gnb notebook](examples/monitoring/multi-gnb-2du/test.ipynb) shows the
complete deployment and interactive traffic flow. Deploy monitoring after the
core/RAN and before UE attachment, so the sniffers observe session establishment.
Profiles select the interfaces and components appropriate to each deployment.

For monolithic OAI RFsim, keep the orchestrator-selected stock Open5GS SMF and
UPF images. The supplied normal and ML profiles set `core_kpis: false` because
the custom core KPIs depend on a separate Open5GS build. This does not turn
off the external NGAP, PFCP, mapper or path collectors in the ML profile.

```python
from nador import MonitoringManagement, MonitoringDeployment

monitoring = MonitoringManagement(
    preceding=ran,
    config=MonitoringDeployment.from_yaml_file("monitoring.yaml"),
)
await monitoring.deploy()
```

Pass the latest network manager to keep host addresses and namespaces current.
The Grafana chart manages its credentials; examples use `admin` / `admin`.
Monitoring can be removed independently with `await monitoring.remove()` and
installed again with `await monitoring.deploy()`.

```python
print(await monitoring.get_logs(components=["ue-mapper-api"], quiet=True))
```

`ExperimentRun` saves a traffic timeline, JSON reports and Prometheus exports.
It opens Prometheus access for final collection; Prometheus scrapes continuously.

```python
from nador.artifacts import ExperimentRun

async with ExperimentRun(
    "results/my-run",
    name="my-scenario",
    monitoring=monitoring,
    queries_file="queries.json",
) as experiment:
    with experiment.section("traffic"):
        report = await my_traffic()
    experiment.save_json("traffic.json", report)
```

Each experiment needs a new result directory. The collector finalizes artifacts
on workload failure; it does not automatically save packet captures or every
pod's logs. Building this package does not rebuild its container images.

Default image versions are selected in `src/nador/assets/images.json`.
Standard profiles inherit them; `images` and `components.<name>.image` remain available
for deliberate overrides. Image-update pull requests change only that manifest.

## OCUDU Native RAN Metrics

Set `ran_kpis: true` in the monitoring profile. Before `ran.deploy()`, enable
native JSON telemetry through the upstream component configuration:

```python
from nador import MonitoringDeployment, configure_ran_kpis

config = MonitoringDeployment.from_yaml_file("monitoring.yaml")
configure_ran_kpis(ran, config)
await ran.deploy()
```

Pass the same `config` to `MonitoringManagement` afterward. No OCUDU source
changes, ML settings, or eBPF probes are needed for this telemetry path.
`ran_kpis: false` does not enable streams or install collectors on a fresh run.
Changing it to false does not undo previously installed RAN configuration;
remove monitoring and redeploy the RAN without the helper to disable an existing stream.

The collector subscribes to each component's native remote-control WebSocket
using only `metrics_subscribe`. A headless Service resolves the current source
pod; one Telegraf deployment is installed for each gNB, CU, CU-CP, CU-UP or DU.
This follows upstream's one-pod-per-component deployment model; scaling one
component to multiple replicas requires per-pod subscriptions instead.
`ocudu_metrics_port` defaults to 8001. Keep this remote-control port inside
trusted cluster networks; the collector does not add authentication to OCUDU.

Metrics include `ue_*`, `cell_*`, layer-specific fields and, for compatible
physical RUs, `ofh_*`. Available fields depend on OCUDU's version, enabled layers
and radio mode; no UE data is invented before attachment and no OFH data is
expected from ZMQ. `ran_component`, `ran_role`, `source_namespace`, PCI/RNTI and
cell-index labels preserve source identity. Disconnected native series expire
after 30 seconds rather than remaining indefinitely.

`ocudu_collector_connected`, `ocudu_collector_messages_total`,
`ocudu_collector_last_message_timestamp_seconds`, and
`ocudu_collector_decode_errors_total` distinguish connection health from fresh
metrics. For example, check both connection = 1 and
`time() - ocudu_collector_last_message_timestamp_seconds < 30`.
The existing srsRAN dashboard is selected for OCUDU; OAI keeps its existing path.

Split CU/DU defaults enable application usage on each component, PDCP on
standalone CU-UP, and scheduler, MAC, RLC, DU-low and RU metrics on the DU.
The pinned standalone DU rejects the executor flag, so it is not set there.
Actual PHY and RU output depends on the radio mode and deployed binary.

The initial image is the original `ghcr.io/ziyad-mabrouk/telegraf:v1.0`, pinned
to its registry digest, not a newly rebuilt NADOR image. The packaged
adapter and Telegraf configuration are mounted from a ConfigMap. Its original
build is recorded in
[srsran-helm](https://github.com/turletti/srsran-helm/blob/freetest/docker/Dockerfile.telegraf);
OCUDU documents the subscription in its
[native output reference](https://docs.ocudu.org/user_manual/outputs/).
