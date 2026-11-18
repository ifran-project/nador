# Contract with the monitoring controller image

The planner (`src/nador/planner.py`, `_controller_values`) writes the settings that the
`nador-controller` image reads at runtime. The image is built in
[`ifran-project/nador-images`](https://github.com/ifran-project/nador-images), so the
two repositories must agree on the shapes below. `tests/test_controller_contract.py`
fails when this repository changes them; update the controller in the same release.

## ConfigMap `nador-one-way`

Mounted into the controller pod by the `monitoring-controller` chart.

### `settings.json`

One object. It merges the one-way probe settings with one request per probe family
(the names are the controller's and do not follow the profile's metric names):

| Key | Type | Meaning |
| --- | --- | --- |
| `image`, `sample`, `test_port`, `n3`, `upf_transit` | from `metrics.n3_one_way`, `metrics.upf_transit`, `tuning.one_way_sample` | One-way probe image and settings. |
| `one_way_targets` | list of targets, optional | Present when one-way probes need explicit anchors (several gNBs/UPFs, or no gNB). |
| `rtt` | request | RTT probes. Also has `imsis`, `slices` and, with packet pairing, `pairer_image`. |
| `throughput` | request | N3 throughput probes on UPFs. |
| `user_plane_paths` | request | Path counters. Also has `port` and `interval`. |
| `extended` | `{probe kind: bool}` | Startup state of each dedicated probe (`cpu`, `drops`, ...). |

Every request has `enabled` (the profile asked for it), `allowed` (the targets make it
possible) and `image`. With several gNBs or UPFs it also has `targets`, a list of targets.

### `targets.json`

`{role: target}` for `gnb` and `upf` on single-anchor deployments, and `{}` when there
are several gNBs or UPFs (the requests then carry `targets` instead).

### Target objects

Every target, in `targets.json` or in a `targets` list, is a `ResolvedTarget` as JSON:

`category`, `component`, `role`, `interface`, `container`, `selector`, `metrics_port`,
`metrics_path`, `latency_mode`, `n3_interface`, `n6_interface`, `n3_network_names`,
`namespace`, `implementation`, `label_selectors`.

## Other names the controller relies on

- Workloads it may pause and resume: `extendedProbeWorkloads` in the chart values, from
  `probes.workload()`: DaemonSets `probe-<kind>-extended-node`, Deployment
  `probe-measurement-quality-extended-service`.
- Environment: `MONITORING_NS`, `UE_MAPPER_URL`, and with a public API
  `NADOR_CONTROLLER_API_PUBLIC`, `NADOR_CONTROLLER_API_CERT`, `NADOR_CONTROLLER_API_KEY`,
  `NADOR_CONTROLLER_API_TOKEN`.
- HTTP API used by `nador-controller` (`probe_control_cli.py`): `GET /v1/probes` and
  `PUT /v1/probes/<probe>` with `enabled`, and optionally `imsis`, `slices`,
  `log_samples`.
