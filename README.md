# NextG Lens

Monitoring and experiment artifacts for networks deployed with the
[SLICES 5G orchestrator](https://gitlab.inria.fr/slices-ri/blueprints/post-5g/orchestrator).
NextG Lens is an independent Python extension: it depends on upstream without
shipping or modifying upstream's Python package or Ansible roles.

It integrates Prometheus, Grafana and Loki, correlates UE/slice/tunnel/session
information, deploys latency and N3 throughput probes, and exports experiment
timelines and metrics. Network deployment remains handled by upstream managers.

Install from GitHub:

```bash
python -m pip install --upgrade "git+https://github.com/yassir63/nextg-lens.git@main"
```

```python
from nextg_lens import MonitoringManagement, MonitoringDeployment
```

Start with the [minimal tutorial](examples/quickstart/tutorial.ipynb): installation,
imports, monitoring deployment and one experiment with artifact collection.

Default monitoring image versions live in [`assets/images.json`](src/nextg_lens/assets/images.json).
Image builds can propose updates through a pull request; deployments change after you merge it.
Explicit scenario overrides still take precedence.

Examples are under [examples/monitoring](examples/monitoring):

- [RFsim](examples/monitoring/rfsim): monolithic and split OAI radios.
- [Multi-gNB](examples/monitoring/multi-gnb-2du/test.ipynb): 20 UEs, a monolithic
  gNB plus a CU/two DUs, three slices and two UPFs; includes on-demand traffic.
- [Open5GS throughput](examples/monitoring/throughput/test.ipynb) and
  [OAI throughput](examples/monitoring/throughput-oai/test.ipynb): small deployments
  using stock core images and the N3 throughput probe.
- [OCUDU ZMQ](examples/monitoring/ocudu-zmq), [B210](examples/monitoring/b210)
  and [R2lab](examples/monitoring/r2lab): additional deployment configurations.

Copyright (c) 2026 Yassir Amami. The extension is licensed under the
[BSD 3-Clause Clear License](LICENSE). Reused/adapted upstream example and chart
material retains the [SLICES orchestrator notice](LICENSES/SLICES-orchestrator.txt).
External dependencies and container images retain their own licenses.
