"""Stop monitoring capture processes during removal; never mounted into the controller."""

import logging
import os
import time

import k8scontroller as controller
import kubernetes


def stop_sniffers(api, pod, namespace, owner):
    """Signal only processes carrying the monitoring owner's explicit marker."""
    from kubernetes.stream import stream

    command = """import os, pathlib, signal
marker = ('MONITORING_SNIFFER_OWNER=' + os.environ['MONITORING_SNIFFER_OWNER']).encode()
for path in pathlib.Path('/proc').glob('[0-9]*/environ'):
    try:
        pid = int(path.parent.name)
        if pid != os.getpid() and marker in path.read_bytes().split(b'\\0'):
            os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, FileNotFoundError):
        pass
"""
    statuses = {s.name: s for s in (pod.status.ephemeral_container_statuses or [])}
    names = []
    for c in pod.spec.ephemeral_containers or []:
        if not c.name.startswith(("amf-sniffer-", "smf-sniffer-")):
            continue
        env = {e.name: e.value for e in c.env or []}
        if env.get("MONITORING_SNIFFER_OWNER") != owner:
            continue
        status = statuses.get(c.name)
        if status and status.state.terminated:
            continue
        if not status or not status.state.running:
            raise RuntimeError(f"Sniffer {c.name} is pending; cannot confirm safe cleanup")
        names.append(c.name)
        stream(
            api.connect_get_namespaced_pod_exec,
            pod.metadata.name,
            namespace,
            container=c.name,
            command=["python", "-c", command],
            stderr=True,
            stdout=True,
            tty=False,
        )
    deadline = time.monotonic() + 30
    while names:
        current = api.read_namespaced_pod(pod.metadata.name, namespace)
        states = {s.name: s for s in current.status.ephemeral_container_statuses or []}
        names = [n for n in names if n not in states or not states[n].state.terminated]
        if names and time.monotonic() >= deadline:
            raise RuntimeError(f"Sniffers did not stop: {names}")
        if names:
            time.sleep(1)


def stop_controller(api, namespace, name="monitoring-controller"):
    """Prevent reconciliation from recreating probes while the release is removed."""
    apps = kubernetes.client.AppsV1Api()
    try:
        apps.patch_namespaced_deployment(name, namespace, {"spec": {"replicas": 0}})
    except kubernetes.client.exceptions.ApiException as exc:
        if exc.status != 404:
            raise
    deadline = time.monotonic() + int(os.environ["CLEANUP_TIMEOUT_SECONDS"])
    while api.list_namespaced_pod(namespace, label_selector=f"app={name}").items:
        if time.monotonic() >= deadline:
            raise RuntimeError("Monitoring controller did not stop before cleanup")
        time.sleep(1)


if __name__ == "__main__":
    kubernetes.config.load_incluster_config()
    api = kubernetes.client.CoreV1Api()
    logger = logging.getLogger("monitoring-cleanup")
    owner = os.environ["MONITORING_NS"]
    stop_controller(api, owner)
    throughput = os.environ.get("THROUGHPUT_ENABLED") == "true"
    namespaces = sorted(set(filter(None, os.environ["CAPTURE_NAMESPACES"].split(","))))
    for namespace in namespaces:
        for pod in api.list_namespaced_pod(namespace).items:
            if pod.status.phase in ("Succeeded", "Failed"):
                continue
            ephemerals = pod.spec.ephemeral_containers or []
            has_probes = any(c.name.startswith((controller.PROBE_NAME_PREFIX, "throughput-probe")) for c in ephemerals)
            annotations = pod.metadata.annotations or {}
            owned = any(annotations.get(f"monitoring.blueprint/{kind}-owner") == owner
                        for kind in ("packet", "latency", "throughput"))
            has_sniffers = any(
                any(e.name == "MONITORING_SNIFFER_OWNER" and e.value == owner for e in c.env or []) for c in ephemerals
            )
            if not (owned or has_probes or has_sniffers):
                continue
            stop_sniffers(api, pod, namespace, owner)
            if not controller.kill_probe_container(pod.metadata.name, namespace, logger):
                raise RuntimeError(f"Probe cleanup failed: {namespace}/{pod.metadata.name}")
            deadline = time.monotonic() + 30
            while controller.list_active_probe_containers(pod.metadata.name, namespace, logger):
                if time.monotonic() > deadline:
                    raise RuntimeError(f"Probe did not stop: {namespace}/{pod.metadata.name}")
                time.sleep(1)
            if throughput:
                if not controller.kill_probe_container(pod.metadata.name, namespace, logger,
                                                       prefix="throughput-probe", stop_pattern="[/]app/exporter.py"):
                    raise RuntimeError(f"Throughput cleanup failed: {namespace}/{pod.metadata.name}")
                deadline = time.monotonic() + 30
                while controller.list_active_probe_containers(pod.metadata.name, namespace, logger,
                                                               prefix="throughput-probe"):
                    if time.monotonic() > deadline:
                        raise RuntimeError(f"Throughput probe did not stop: {namespace}/{pod.metadata.name}")
                    time.sleep(1)
            if owned:
                api.patch_namespaced_pod(
                    pod.metadata.name,
                    namespace,
                    {
                        "metadata": {
                            "annotations": {
                                "monitoring.blueprint/latency-port": None,
                                "monitoring.blueprint/latency-owner": None,
                                "monitoring.blueprint/packet-owner": None,
                                "monitoring.blueprint/throughput-owner": None,
                                "monitoring.blueprint/throughput-port": None,
                            }
                        }
                    },
                )

    # Kopf timers add this marker to watched pods, including pods without probes.
    # Release it only after capture cleanup, since the stopped controller cannot.
    for namespace in namespaces:
        for pod in api.list_namespaced_pod(namespace).items:
            finalizers = pod.metadata.finalizers or []
            markers = {"kopf.zalando.org/KopfFinalizerMarker"}
            if throughput:
                markers.add("monitoring.blueprint/throughput")
            if markers.intersection(finalizers):
                api.patch_namespaced_pod(
                    pod.metadata.name,
                    namespace,
                    [
                        {"op": "<YOUR_PASSWORD>", "path": "/metadata/resourceVersion", "value": pod.metadata.resource_version},
                        {
                            "op": "replace",
                            "path": "/metadata/finalizers",
                            "value": [f for f in finalizers if f not in markers],
                        },
                    ],
                )
