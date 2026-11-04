# Monitoring

The [multi-gnb notebook](examples/monitoring/multi-gnb-2du/test.ipynb) shows the
complete deployment and interactive traffic flow. Deploy monitoring after the
core/RAN and before UE attachment, so the sniffers observe session establishment.
Profiles select the interfaces and components appropriate to each deployment.

```python
from fiveg_lens import MonitoringManagement, MonitoringDeployment

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
from fiveg_lens.artifacts import ExperimentRun

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
