# Monitoring

Follow the [minimal tutorial](examples/quickstart/tutorial.ipynb) for installation,
configuration, deployment and artifact collection in one notebook.

Install the latest merged library from GitHub `main` in your notebook:

```python
%pip install --upgrade "git+https://github.com/yassir63/nador.git@main"
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

The `rnti-mapper` component (enabled by default, `rnti_mapper: false` to remove it)
exports `ran_ue_info{ran_node,ran_component,rnti,ran_ue_id,imsi,ue_name} 1`, so that the
OAI dashboard shows UE names next to the RNTIs and lists them in the "UE identities" table.
Every 10 seconds it reads the UE contexts of the RRC of each gNB / CU / CU-CP (OAI
`nrRRC_stats.log` from the `metrics-parser` sidecar, OCUDU log lines `c-rnti=`/`ran_ue=`),
joins them with the AMF log (OAI or Open5GS) and, when deployed, the `ue-mapper-api`
inventory, and names the IMSIs after the UEs of the metamodel. RAN UE NGAP ids are
allocated per RAN node: with several RRC nodes (a gNB and a CU, for example), the inventory
binds each id to its node through the node pod IPs; without it, an id used by several
nodes at once is shown as `imsi="ambiguous"` rather than guessed.

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
