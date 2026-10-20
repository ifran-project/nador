# Monitoring extension

Follow [the OAI RFsim notebook](examples/monitoring/rfsim/oai-rfsim/<YOUR_PASSWORD>.ipynb) for the complete example.
It deploys monitoring after the core and RAN and before UE attachment. Copy or
adjust [monitoring.yaml](examples/monitoring/rfsim/oai-rfsim/monitoring.yaml) for your deployment.
The sample uses `eth0` for VM packet capture; choose the interfaces appropriate
to your deployment. The Grafana Helm chart creates and manages its credential
Secret with username `admin` and password `admin`.

```python
from orchestrator_monitoring import MonitoringManagement
from orchestrator_monitoring.models import MonitoringDeployment

monitoring = MonitoringManagement(preceding=ran)
monitoring.deployment_model.monitoring = MonitoringDeployment.from_yaml_file("monitoring.yaml")
await monitoring.deploy()
```

Pass the la<YOUR_PASSWORD> manager so SSH addresses and component namespaces are current.
Save the deployment model in a `finally` block to retain cleanup state after a
partial failure. Component logs are available on monitoring too:

```python
print(await monitoring.get_logs(component="ue-mapper-api"))
```

`ExperimentRun` records section timings, JSON reports and Prometheus exports.
It reuses the exporter and timeline formats from `5g_ansible` develop. It opens
the Prometheus tunnel during final collection and closes it afterwards;
Prometheus itself scrapes continuously in the cluster.

```python
from orchestrator_monitoring.artifacts import ExperimentRun

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

Temporary image and chart adaptations are visible notebook cells between
`prepare()` and `install()`, tagged `temporary-network-patch`. The library does
not apply them automatically.

The example custom Open5GS SMF image must export
`fivegs_smffunction_sm_seid_session{seid,snssai}` for slice throughput joins with
the UPF per-session counters. Kubernetes readiness alone does not verify those
metrics or UE mappings. Probe images are configured separately in the profile;
building this Python package does not rebuild those images.
