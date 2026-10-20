# Orchestrator monitoring

An independent monitoring and experiment-artifact package for the original
[SLICES 5G orchestrator](https://gitlab.inria.fr/slices-ri/blueprints/post-5g/orchestrator).
It depends on an upstream revision; it does not ship or modify the `orchestrator`
Python package or its Ansible roles.

`orchestrator_monitoring` provides monitoring deployment, component logs and
Prometheus artifact collection. OAI statistics parsers use local custom RAN charts;
temporary Open5GS patches remain explicit notebook cells. The custom examples live under `examples/monitoring`; physical trials
live separately under `examples/monitoring-trials`.

Prometheus, Grafana, Loki and Promtail use pinned published Helm charts. Local
charts provide the custom monitoring components; `monitoring-integration` contains
our dashboards and metrics Services. Shared Helm helpers are copied during local
chart preparation, without chart dependencies or dependency builds.

The existing `orchestrator` imports for core, RAN, provisioning and Kubernetes
remain upstream imports. Monitoring imports use `orchestrator_monitoring`.

See [the integration boundary](README-UPSTREAM-CHANGES.md) for what the extension
owns and how weekly builds select upstream revisions.
