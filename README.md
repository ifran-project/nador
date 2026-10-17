# Post-5G Orchestrator

Please refer to [these tutorials](./examples/tutorials/README.md) for a preliminary documentation of the Post-5G orchestrator and its usage.


## Component logs

Discover container names before selecting one. The result is always a dictionary
keyed by component, including regular, init and ephemeral containers:

```python
print(await ran.list_containers(components=["gnb"], quiet=True))
# {"gnb": ["gnb", "metrics-parser"]}
```

Select a container within a component, or omit `container` to read all containers:

```python
print(await ran.get_logs(components=["gnb"], container="metrics-parser", quiet=True))
print(await ran.get_logs(components=["gnb"], quiet=True))
```

A single component returns log text directly. Multiple components return a
dictionary keyed by component name:

```python
logs = await core.get_logs(components=["amf", "upf"], quiet=True)
print(logs["amf"])
```

Previously, single-component results also required indexing, such as
`(await ran.get_logs())["gnb"]`. Use `await ran.get_logs(components=["gnb"])`
with this API. Container discovery and selection are supported by core, RAN, RIC and
containerized UE managers.

Monitoring and experiment artifacts: [documentation](MONITORING.md).
