# 5G Lens

Monitoring and experiment artifacts for networks deployed with the
[SLICES 5G orchestrator](https://gitlab.inria.fr/slices-ri/blueprints/post-5g/orchestrator).
5G Lens is an independent Python extension: it depends on upstream without
shipping or modifying upstream's Python package or Ansible roles.

It integrates Prometheus, Grafana and Loki, correlates UE/slice/tunnel/session
information, deploys latency and N3 throughput probes, and exports experiment
timelines and metrics. Network deployment remains handled by upstream managers.
The Python import remains `orchestrator_monitoring`.

Examples are under [examples/monitoring](examples/monitoring):

- [RFsim](examples/monitoring/rfsim): monolithic and split OAI radios.
- [Hackathon](examples/monitoring/hackathon-2du/test.ipynb): 20 UEs, a monolithic
  gNB plus a CU/two DUs, three slices and two UPFs; includes on-demand traffic.
- [Open5GS throughput](examples/monitoring/throughput/test.ipynb) and
  [OAI throughput](examples/monitoring/throughput-oai/test.ipynb): small deployments
  using stock core images and the N3 throughput probe.
- [OCUDU ZMQ](examples/monitoring/ocudu-zmq), [B210](examples/monitoring/b210)
  and [R2lab](examples/monitoring/r2lab): additional deployment configurations.

See [MONITORING.md](MONITORING.md) for the monitoring API and artifact collection.
Container image tags and digests are recorded in [IMAGE_VERSIONS.json](IMAGE_VERSIONS.json).
The examples are adapted for orchestrator 0.0.5 and metamodel 0.0.5. The dependency
tracks upstream `main`; physical-radio examples explicitly select vendor images
as a temporary workaround for its 0.0.5 vendor-image lookup issue.

Copyright (c) 2026 Yassir Amami. The extension is licensed under the
[BSD 3-Clause Clear License](LICENSE). Reused/adapted upstream example and chart
material retains the [SLICES orchestrator notice](LICENSES/SLICES-orchestrator.txt).
External dependencies and container images retain their own licenses.
