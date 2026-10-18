"""Resolve model components and build declarative monitoring resources."""

import hashlib
import json
from pathlib import Path

from orchestrator.models.deployment import DeploymentModel
from orchestrator.utils.k8s import build_k8s_safe

ASSETS = Path(__file__).resolve().parents[1] / "monitoring_assets"
OWNER_LABEL = "post5g.blueprint/monitoring"


def release_names(namespace: str) -> dict[str, str]:
    """Readable, namespace-specific Helm release names."""
    prefix = (
        namespace
        if len(namespace) <= 30
        else namespace[:19] + "-" + hashlib.sha256(namespace.encode()).hexdigest()[:10]
    )
    return {"stack": prefix + "-prometheus", "grafana": prefix + "-grafana", "loki": prefix + "-loki"}


def resolve_targets(model: DeploymentModel) -> list[dict]:
    """Resolve stable selectors from the same release naming used by HelmManagement."""
    result = []
    for target in model.monitoring.targets:
        components = model.ues if target.category == "ue" else getattr(model, target.category).components
        if target.component not in components:
            raise ValueError(f"Unknown {target.category} component {target.component!r}")
        component = components[target.component]
        namespace = getattr(component, "namespace", None)
        if not namespace:
            raise ValueError(f"{target.component}: namespace is unresolved; pass the la<YOUR_PASSWORD> deployed manager")
        if target.role:
            expected = {"amf": {"amf"}, "smf": {"smf"}, "upf": {"upf"}, "gnb": {"gnb", "cu", "cu_up"}}
            if component.type not in expected[target.role]:
                raise ValueError(f"{target.component}: {component.type} cannot be a {target.role} packet target")
        result.append(
            target.model_dump()
            | {
                "namespace": namespace,
                "implementation": str(component.implementation),
                "selector": target.selector
                or {"app": build_k8s_safe(f"{component.implementation}-{target.component}")},
                "label_selectors": [
                    f"{k}={v}"
                    for k, v in (
                        target.selector or {"app": build_k8s_safe(f"{component.implementation}-{target.component}")}
                    ).items()
                ],
                "target_container": target.container,
                "iface": target.interface,
            }
        )
    return result


def metadata(name: str, namespace: str, owner: str) -> dict:
    return {"name": name, "namespace": namespace, "labels": {OWNER_LABEL: owner}}


def service(name: str, namespace: str, owner: str, selector: dict, port: int, path: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": metadata(name, namespace, owner)
        | {
            "annotations": {"prometheus.io/scrape": "true", "prometheus.io/port": str(port), "prometheus.io/path": path}
        },
        "spec": {"selector": selector, "ports": [{"name": "metrics", "port": port, "targetPort": port}]},
    }


def deployment(name: str, namespace: str, image: str, port: int | None = None, env: dict | None = None) -> dict:
    labels = {"app": name, OWNER_LABEL: namespace}
    container = {
        "name": name,
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "env": [{"name": k, "value": str(v)} for k, v in (env or {}).items()],
    }
    if port:
        container["ports"] = [{"containerPort": port}]
        container["readinessProbe"] = {"tcpSocket": {"port": port}, "initialDelaySeconds": 3, "periodSeconds": 5}
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": metadata(name, namespace, namespace),
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": labels},
            "template": {"metadata": {"labels": labels}, "spec": {"containers": [container]}},
        },
    }


def resources(model: DeploymentModel, targets: list[dict]) -> list[dict]:
    """Build manifests with component-specific namespaces and selectors."""
    cfg = model.monitoring
    ns = cfg.namespace
    docs = []
    for i, target in enumerate(targets):
        if target["metrics_port"]:
            docs.append(
                service(
                    "post5g-metrics-" + hashlib.sha256(f"{ns}:{i}".encode()).hexdigest()[:12],
                    target["namespace"],
                    ns,
                    target["selector"],
                    target["metrics_port"],
                    target["metrics_path"],
                )
            )
    prometheus = f"http://{release_names(ns)['stack']}-prometheus.{ns}.svc:9090"
    for enabled, implementation, image in [
        (cfg.core_kpis, "open5gs", "ghcr.io/ziyad-mabrouk/kpi-calculator-open5gs:v1.0.0-standard"),
        (cfg.ran_kpis, "oai", "ghcr.io/ziyad-mabrouk/kpi-calculator-oai:v1.0.1-standard"),
    ]:
        if not enabled:
            continue
        category = "core" if implementation == "open5gs" else "ran"
        if str(getattr(model, category).implementation) != implementation:
            raise ValueError(f"{category} KPI calculator requires {implementation}")
        if not any(t["category"] == category and t["metrics_port"] for t in targets):
            raise ValueError(f"{category} KPI calculator requires an explicit metrics endpoint target")
        name = f"kpi-calculator-{implementation}"
        d = deployment(
            name,
            ns,
            image,
            9000,
            {
                "UPDATE_PERIOD": "1",
                "TIME_RANGE": "30s",
                "MONITORING_PROMETHEUS_URL": prometheus,
                "MONARCH_THANOS_URL": prometheus,
            },
        )
        d["spec"]["template"]["spec"]["containers"][0]["command"] = ["python", "-u", "kpi_calculator.py"]
        docs += [d, service(f"{name}-service", ns, ns, {"app": name}, 9000, "/metrics")]
    if not cfg.latency:
        return docs
    docs += [
        deployment("redis", ns, "redis:7", 6379),
        service("redis", ns, ns, {"app": "redis"}, 6379, "/metrics"),
        deployment("ue-mapper-api", ns, cfg.mapper_image, 8080, {"REDIS_HOST": f"redis.{ns}.svc.cluster.local"}),
        service("ue-mapper-api", ns, ns, {"app": "ue-mapper-api"}, 8080, "/metrics"),
    ]
    # Redis has no HTTP exporter. Remove the scrape annotations on its transport service.
    docs[-3]["metadata"].pop("annotations")
    controller_data = {name: (ASSETS / name).read_text() for name in ["controller.py", "targets.py"]}
    docs.append(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": metadata("post5g-monitoring-code", ns, ns),
            "data": controller_data,
        }
    )
    docs.append({"apiVersion": "v1", "kind": "ServiceAccount", "metadata": metadata("post5g-monitoring", ns, ns)})
    # Scoped role bindings for each watched namespace plus the addon namespace.
    for namespace in sorted({ns} | {t["namespace"] for t in targets}):
        name = f"post5g-monitoring-{ns}"[:63]
        docs.append(
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": metadata(name, namespace, ns),
                "rules": [
                    {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch", "patch"]},
                    {"apiGroups": [""], "resources": ["pods/ephemeralcontainers"], "verbs": ["patch", "update"]},
                    {"apiGroups": [""], "resources": ["pods/exec"], "verbs": ["create", "get"]},
                    {"apiGroups": [""], "resources": ["events"], "verbs": ["create", "patch", "update"]},
                    {
                        "apiGroups": [""],
                        "resources": ["services"],
                        "verbs": ["get", "list", "create", "patch", "update"],
                    },
                    {
                        "apiGroups": ["apps"],
                        "resources": ["deployments"],
                        "verbs": ["get", "list", "create", "patch", "update"],
                    },
                ],
            }
        )
        docs.append(
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": metadata(name, namespace, ns),
                "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
                "subjects": [{"kind": "ServiceAccount", "name": "post5g-monitoring", "namespace": ns}],
            }
        )
    env = {
        "MONITORING_NS": ns,
        "CORE_NS": ns,
        "PROBE_TARGETS": json.dumps(targets),
        "PROBE_NAME_PREFIX": "post5g-latency-probe",
        "UE_MAPPER_URL": f"http://ue-mapper-api.{ns}.svc.cluster.local:8080",
        "PROBE_IMAGE": cfg.probe_image,
        "PACKET_PAIRING": "1" if cfg.packet_pairing else "0",
        "PACKET_PAIRER_IMAGE": cfg.pairer_image,
        "PACKET_PAIRER_NAME": "post5g-packet-pairer",
        "PACKET_PAIRER_SERVICE_NAME": "post5g-packet-pairer",
        "AMF_SNIFFER_IMAGE": cfg.amf_sniffer_image,
        "SMF_SNIFFER_IMAGE": cfg.smf_sniffer_image,
        "REDIS_HOST": f"redis.{ns}.svc.cluster.local",
    }
    d = deployment("post5g-monitoring-controller", ns, cfg.controller_image, env=env)
    pod = d["spec"]["template"]
    pod["metadata"]["annotations"] = {"checksum/code": hashlib.sha256(json.dumps(controller_data).encode()).hexdigest()}
    pod["spec"]["serviceAccountName"] = "post5g-monitoring"
    pod["spec"]["volumes"] = [{"name": "code", "configMap": {"name": "post5g-monitoring-code"}}]
    c = pod["spec"]["containers"][0]
    c["volumeMounts"] = [{"name": "code", "mountPath": "/post5g", "readOnly": True}]
    c["command"] = ["kopf", "run", "--standalone"]
    for namespace in sorted({t["namespace"] for t in targets if t["role"]}):
        c["command"] += ["--namespace", namespace]
    c["command"] += ["/post5g/controller.py"]
    docs.append(d)
    return docs
