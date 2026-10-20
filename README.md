# Orchestrator monitoring

An independent monitoring and experiment-artifact package for the original
[SLICES 5G orchestrator](https://gitlab.inria.fr/slices-ri/blueprints/post-5g/orchestrator).
It depends on an upstream revision; it does not ship or modify the `orchestrator`
Python package or its Ansible roles.

`orchestrator_monitoring` provides monitoring deployment, component logs and
Prometheus artifact collection. Temporary OAI/Open5GS
chart patches are explicit notebook cells, outside the package. The custom examples live under `examples/monitoring`; physical trials
live separately under `examples/monitoring-trials`.

The existing `orchestrator` imports for core, RAN, provisioning and Kubernetes
remain upstream imports. Monitoring imports use `orchestrator_monitoring`.

See [the integration boundary](README-UPSTREAM-CHANGES.md) for what the extension
owns and how weekly builds select upstream revisions.
