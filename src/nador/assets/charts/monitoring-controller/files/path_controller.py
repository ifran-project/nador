"""Reconcile passive path probes independently of UE attachment state."""
import hashlib
import json
import logging
import os
import time

from kubernetes import client, config

LOG = logging.getLogger("path-controller")


def container_spec(target, pod, settings, owner):
    network_names = target.get("n3_network_names", [])
    interface = target.get("n3_interface") or (
        target.get("interface") or "" if target["role"] == "upf" and not network_names else "")
    values = {
        "MONITORING_SNIFFER_OWNER": owner,
        "USER_PLANE_PROBE_ANCHOR": target["role"],
        "USER_PLANE_PROBE_METRICS_PORT": str(settings["path_port"]),
        "USER_PLANE_PROBE_COLLECTION_INTERVAL_SECONDS": str(settings["path_interval_seconds"]),
        "USER_PLANE_PROBE_NETWORK_STATUS_JSON": (pod.metadata.annotations or {}).get("k8s.v1.cni.cncf.io/network-status", "[]"),
        "USER_PLANE_PROBE_N3_NETWORK_NAMES": json.dumps(network_names),
        "USER_PLANE_PROBE_N3_INTERFACE": interface,
        "USER_PLANE_PROBE_N6_INTERFACE": target.get("n6_interface") or "",
        "USER_PLANE_PROBE_MONITOR_N6": str(target["role"] == "upf").lower(),
    }
    spec = {"image": settings["path_image"], "imagePullPolicy": "IfNotPresent",
            "targetContainerName": target["container"],
            "env": [{"name": key, "value": value} for key, value in values.items()],
            "securityContext": {"runAsUser": 0, "capabilities": {"add": ["NET_RAW"]}}}
    fingerprint = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:12]
    spec["env"].append({"name": "MONITORING_PATH_CONFIG", "value": fingerprint})
    return spec


def reconcile(api, target, settings, owner):
    selector = ",".join(f"{key}={value}" for key, value in target["selector"].items())
    for pod in api.list_namespaced_pod(target["namespace"], label_selector=selector).items:
        if pod.status.phase != "Running" or pod.metadata.deletion_timestamp:
            continue
        if target["container"] not in {item.name for item in pod.spec.containers}:
            raise ValueError(f"Target container missing in {pod.metadata.name}")
        spec = container_spec(target, pod, settings, owner)
        existing = pod.spec.ephemeral_containers or []
        terminated = {item.name for item in (pod.status.ephemeral_container_statuses or []) if item.state.terminated}
        active = [item for item in existing if item.name.startswith("user-plane-path-") and item.name not in terminated]
        if active:
            expected = {item["name"]: item["value"] for item in spec["env"]}
            for item in active:
                actual = {entry.name: entry.value for entry in (item.env or [])}
                if actual.get("MONITORING_SNIFFER_OWNER") != owner or actual.get("MONITORING_PATH_CONFIG") != expected["MONITORING_PATH_CONFIG"]:
                    raise ValueError(f"Active path probe configuration differs in {pod.metadata.name}; remove monitoring before changing it")
            continue
        spec["name"] = f"user-plane-path-{len(existing) + 1}"
        body = {"metadata": {"resourceVersion": pod.metadata.resource_version},
                "spec": {"ephemeralContainers": [api.api_client.sanitize_for_serialization(item) for item in existing] + [spec]}}
        api.patch_namespaced_pod_ephemeralcontainers(pod.metadata.name, target["namespace"], body)
        LOG.info("Injected path probe in %s/%s", target["namespace"], pod.metadata.name)


def main():
    logging.basicConfig(level=logging.INFO)
    config.load_incluster_config()
    api = client.CoreV1Api()
    settings = json.loads(os.environ["OBSERVABILITY_SETTINGS"])
    targets = json.loads(os.environ["MONITORING_TARGETS"])
    while True:
        for target in targets:
            if target["role"] not in ("gnb", "upf"):
                continue
            try:
                reconcile(api, target, settings, os.environ["MONITORING_OWNER"])
            except Exception:
                LOG.exception("Path reconciliation failed for %s", target["component"])
        time.sleep(5)


if __name__ == "__main__":
    main()
