# Controlling probes on a running deployment

Two levels of control work without redeploying the whole stack.

## Start and stop

The controller manages `rtt`, `throughput`, `n3_one_way`, `upf_transit`, `user_plane_paths`,
`split_signaling` and the host probes. A metric deployed with `on_demand: true` starts stopped.

```python
await monitoring.start("rtt", slices=["01:ffffff"], at=["upf"])  # one slice, at the UPF only
await monitoring.start("rtt", slices=[], at=["gnb", "upf"])      # every UE again, both anchors
await monitoring.start("scheduler_wait")
await monitoring.stop("rtt")
```

`start` also changes the scope of a running metric:

- `imsis` and `slices` narrow `rtt` and `throughput` to some UEs; an empty list means all.
- `at` moves `rtt` between `["gnb"]`, `["upf"]` and both (not with `packet_pairing`).
- `log_samples` turns per-sample logging of `n3_one_way` on or off.

The change is written to the controller's settings and applied within a few seconds. `stop` keeps
everything installed and removes only the probe's running pods: controller-created pods are
deleted, host-probe DaemonSets are paused with a node selector no node matches, and the
measurement-quality Deployment is scaled to zero. `start` brings them back in a few seconds.

Bad requests are refused before anything changes: an unknown or undeployed metric, an option the
metric does not take, an invalid IMSI or slice, a measuring point with no target, a probe this RAN
does not run (split signaling without a CU/DU split).

## Deploy and remove a single metric

To add a metric that is not deployed, change its options, or remove it completely:

```python
await monitoring.deploy_probe("scheduler_wait", {"on_demand": False, "targets": []})
await monitoring.deploy_probe("core_logs", True)
await monitoring.remove_probe("scheduler_wait")
```

Options are written as in the profile, every one of them. NADOR plans the deployment again and
changes only what is affected: a host probe installs its own release, a new `rtt` option only
upgrades the controller, and the first metric that needs session tracking also installs the
controller, UE mapper and Redis and attaches the sniffers (UEs attached before that are seen once
they reattach). `remove_probe` uninstalls whatever nothing else needs any more. In code, the
per-group `logs` metrics are called `ran_logs`, `core_logs` and `host_logs`.

| | `stop` / `start` | `remove_probe` / `deploy_probe` |
| --- | --- | --- |
| Helm release | kept | uninstalled / installed |
| Running pods | removed / recreated | removed / created |
| How long it takes | a few seconds | as long as Helm needs, at most its timeout |
| When to use it | turning a metric on and off during experiments | adding, reconfiguring or removing a metric |

## Saving and restoring the handle

`monitoring.output(metamodel_file=..., deployment_model_file=...)` saves both deployment models.
After a kernel restart, `MonitoringManagement.from_yaml_files(metamodel_file,
deployment_model_file)` restores the handle without redeploying. The saved models describe the
current state, including metrics started or stopped since deployment. Keep them private.
