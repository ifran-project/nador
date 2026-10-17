# Monitoring fork

The `monitoring` branch combines the container logging contribution with local
monitoring and experiment artifacts. Keep upstream contributions on their own
branches; merge them here when needed. The original `container-log-selection`
branch remains suitable for its upstream merge request.

## Use the library

Use Python 3.13 and install this branch into your notebook's environment:

```bash
python -m pip install --upgrade "git+https://gitlab.inria.fr/yamami/orchestrator.git@monitoring" \
  --extra-index-url https://gitlab.inria.fr/api/v4/projects/70914/packages/pypi/simple
```

Restart the notebook kernel after upgrading. An existing editable installation
points to its local checkout; uninstall it first if switching to published code.
The metamodel dependency is unchanged.

Follow [the default notebook](examples/monitoring/default/<YOUR_PASSWORD>.ipynb) for the complete example.
It deploys monitoring after the core and RAN and before UE attachment. Copy or
adjust [monitoring.yaml](examples/monitoring/default/monitoring.yaml) for your deployment.
The sample uses `eth0` for VM packet capture; choose the interfaces appropriate
to your deployment. The Grafana Helm chart creates and manages its credential
Secret with username `admin` and password `admin`.

```python
from orchestrator.deployment import MonitoringManagement
from orchestrator.models.monitoring import MonitoringDeployment

monitoring = MonitoringManagement(preceding=ran)
monitoring.deployment_model.monitoring = MonitoringDeployment.from_yaml_file("monitoring.yaml")
await monitoring.deploy()
```

Pass the la<YOUR_PASSWORD> manager so SSH addresses and component namespaces are current.
Save the deployment model in a `finally` block to retain cleanup state after a
partial failure. Logs and container discovery are available on monitoring too:

```python
print(await monitoring.list_containers(component="ue-mapper-api"))
print(await monitoring.get_logs(component="ue-mapper-api"))
```

`ExperimentRun` records section timings, JSON reports and Prometheus exports.
It reuses the exporter and timeline formats from `5g_ansible` develop. It opens
the Prometheus tunnel during final collection and closes it afterwards;
Prometheus itself scrapes continuously in the cluster.

```python
from orchestrator.artifacts import ExperimentRun

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

Use a new result directory for each experiment. Workload failures still finalize
the timeline; collection failures are recorded in experiment metadata. The
collector does not automatically collect pcaps or every pod's logs.

The example custom Open5GS SMF image must export
`fivegs_smffunction_sm_seid_session{seid,snssai}` for slice throughput joins with
the UPF per-session counters. Kubernetes readiness alone does not verify those
metrics or UE mappings. Probe images are configured separately in the profile;
building this Python package does not rebuild those images.
