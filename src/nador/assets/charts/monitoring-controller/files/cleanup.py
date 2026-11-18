"""Stop monitoring capture processes during removal; never mounted into the controller."""

import os
import time

import kubernetes
from kubernetes.stream import stream

NOT_FOUND = 404


def stop_sniffers(api, pod, namespace, owner):
    """Signal only processes carrying the monitoring owner's explicit marker."""
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
        if not c.name.startswith(("amf-sniffer-", "smf-sniffer-", "user-plane-path-")):
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
        if exc.status != NOT_FOUND:
            raise
    deadline = time.monotonic() + int(os.environ["CLEANUP_TIMEOUT_SECONDS"])
    while api.list_namespaced_pod(namespace, label_selector=f"app={name}").items:
        if time.monotonic() >= deadline:
            raise RuntimeError("Monitoring controller did not stop before cleanup")
        time.sleep(1)


if __name__ == "__main__":
    kubernetes.config.load_incluster_config()
    api = kubernetes.client.CoreV1Api()
    owner = os.environ["MONITORING_NS"]
    stop_controller(api, owner)
    namespaces = filter(None, os.environ["CAPTURE_NAMESPACES"].split(","))
    for namespace in namespaces:
        for pod in api.list_namespaced_pod(namespace).items:
            if pod.status.phase not in ("Succeeded", "Failed"):
                stop_sniffers(api, pod, namespace, owner)
