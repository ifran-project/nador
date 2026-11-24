"""Builders shared by the tests: monitoring profiles, IFRAN networks and a recording Ansible.

Everything NADOR does on a cluster goes through ``run_ansible_role``. Tests replace that one
function with :class:`Ansible`, which records each call, so the rest of NADOR (profile
validation, planning, the manager's decisions) runs exactly as in production.
"""

from __future__ import annotations

import copy
import http.server
import json
import threading
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from ifran.deployment.base import BaseManagement
from ifran.models.deployment import DeploymentModel
from metamodel_3gpp.models.metamodel import MetaModel

from nador import MonitoringManagement
from nador.config import GROUP_OF, Metrics, MonitoringDeployment, profile_key, validate_deployment

TARGETS = {
    "amf": {"category": "core", "component": "amf", "role": "amf", "container": "amf", "interface": "eth0"},
    "smf": {"category": "core", "component": "smf", "role": "smf", "container": "smf", "interface": "eth0"},
    "upf": {"category": "core", "component": "upf", "role": "upf", "container": "upf", "interface": "eth0"},
    "gnb": {"category": "ran", "component": "gnb", "role": "gnb", "container": "gnb", "interface": "eth0"},
}


def options(metric: str, **given: Any) -> dict[str, Any]:
    """A metric as a profile must write it: every option, at its default unless given."""
    model = next(arg for arg in Metrics.model_fields[metric].annotation.__args__ if arg is not type(None))
    defaults = {
        name: info.get_default(call_default_factory=True)
        for name, info in model.model_fields.items()
        if not info.is_required()
    }
    return defaults | given


def profile(*, metrics: dict[str, Any] | None = None, targets: list[str | dict] | None = None, **fields: Any) -> dict:
    """A complete profile with every metric off, except those given by name.

    ``metrics`` maps a metric to ``True`` or to its options (see :func:`options`); ``targets``
    names the standard AMF/SMF/UPF/gNB targets to watch (all four by default) or gives a target
    in full.
    """
    groups: dict[str, dict[str, Any]] = {}
    for name in Metrics.model_fields:
        groups.setdefault(GROUP_OF[name], {})[profile_key(name)] = False
    for name, value in (metrics or {}).items():
        groups[GROUP_OF[name]][profile_key(name)] = value
    chosen = [copy.deepcopy(TARGETS[t] if isinstance(t, str) else t) for t in targets or TARGETS]
    return {"namespace": "monitoring", "metrics": groups, "targets": chosen} | fields


def monitoring(**kwargs: Any) -> MonitoringDeployment:
    """A validated profile (see :func:`profile`), checked as strictly as a monitoring.yaml file."""
    return validate_deployment(profile(**kwargs), explicit=True)


OCUDU_GNB = {
    "metrics": {"enable_json": True},
    "remote_control": {"enabled": True, "bind_addr": "0.0.0.0", "port": 8001},  # noqa: S104
}
RAN_LAYOUTS = {
    "gnb": {"gnb": {"type": "gnb", "config": {"gnbId": 1, "nrCellId": 1}}},
    "cu-du": {
        "cu": {"type": "cu", "config": {"gnbId": 1, "nrCellId": 1}},
        "du": {"type": "du", "config": {"gnbId": 1, "gnbDuId": 1, "nrCellId": 1, "cu_cp": "cu", "cu_up": "cu"}},
    },
    "cucp-cuup-du": {
        "cucp": {"type": "cu_cp", "config": {"gnbId": 1, "nrCellId": 1}},
        "cuup": {"type": "cu_up", "config": {"gnbId": 1}},
        "du": {"type": "du", "config": {"gnbId": 1, "gnbDuId": 1, "nrCellId": 1, "cu_cp": "cucp", "cu_up": "cuup"}},
    },
}


@dataclass
class Network:
    """A deployed 5G network as IFRAN describes it: its meta model and its deployment model."""

    metamodel: MetaModel
    deployment_model: DeploymentModel


def network(*, ran: str = "oai", layout: str = "gnb", upfs: int = 1) -> Network:
    """An Open5GS core and an OAI, OCUDU or UERANSIM RAN on one node, already deployed.

    ``layout`` is ``gnb`` (monolithic), ``cu-du`` (F1 split) or ``cucp-cuup-du`` (F1 and E1);
    ``upfs`` adds UPFs named ``upf``, ``upf2``, ... Every component carries the namespace IFRAN
    records once it is installed.
    """
    core = {name: c.model_dump(mode="json") for name, c in MetaModel().core.components.items() if c.type != "upf"}
    upf_names = ["upf", *(f"upf{index}" for index in range(2, upfs + 1))]
    core |= {name: {"type": "upf"} for name in upf_names}
    radio = copy.deepcopy(RAN_LAYOUTS[layout])
    metamodel = MetaModel.model_validate({"core": {"components": core}, "ran": {"components": radio}})
    deployed = {"node_name": "node1"}
    ran_components = {name: deployed | {"type": c["type"], "namespace": "ran"} for name, c in radio.items()}
    if ran == "ocudu":
        for component in ran_components.values():
            if component["type"] in ("gnb", "cu_cp", "cu"):
                component["advanced_config"] = copy.deepcopy(OCUDU_GNB)
    deployment_model = DeploymentModel.model_validate(
        {
            "nodes": {"node1": {"roles": ["core", "ran"]}},
            "core": {
                "implementation": "open5gs",
                "components": {name: deployed | {"type": c["type"], "namespace": "core"} for name, c in core.items()},
            },
            "ran": {"implementation": ran, "components": ran_components},
            "params": {"k8s": {"master_node": "node1"}},
        }
    )
    return Network(metamodel, deployment_model)


def manager(config: MonitoringDeployment | dict, net: Network | None = None) -> MonitoringManagement:
    """A MonitoringManagement built the way a notebook builds it, after the RAN is deployed."""
    if isinstance(config, dict):
        config = validate_deployment(config, explicit=True)
    net = net or network()
    base = BaseManagement(metamodel=net.metamodel, deployment_model=net.deployment_model)
    return MonitoringManagement(preceding=base, config=config)


@dataclass
class Ansible:
    """Stands in for ``run_ansible_role``: records what NADOR asks the cluster to do."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    fail: tuple[str, str] | None = None  # (helm action, release) that fails, as a broken install would

    async def __call__(self, *, role: str, inputs: dict[str, Any], **_: Any) -> dict[str, Any] | None:
        self.calls.append({"role": role, **inputs})
        if self.fail == (inputs.get("helm_action"), inputs.get("chart_name")):
            raise RuntimeError(f"helm {self.fail[0]} {self.fail[1]} failed")
        if inputs.get("helm_action") == "get_logs":
            return {"logs": {"pod": "log line"}}
        return None

    def helm(self, action: str | None = None) -> list[tuple[str, str]]:
        """``(action, release)`` for every Helm action, or the releases of one action."""
        done = [(c["helm_action"], c["chart_name"]) for c in self.calls if c["role"] == "monitoring/component"]
        return done if action is None else [(a, name) for a, name in done if a == action]

    def releases(self, action: str) -> list[str]:
        return [name for _, name in self.helm(action)]

    def tasks(self) -> list[str]:
        """The control-node tasks run, such as ``prepare`` or ``inject_sniffers``."""
        return [c["monitoring_action"] for c in self.calls if c["role"] == "monitoring/stack"]

    def controller_settings(self) -> list[dict[str, Any]]:
        """Settings patched into the running controller, one entry per start or stop."""
        return [
            c["controller_settings"] for c in self.calls if c.get("monitoring_action") == "patch_controller_settings"
        ]

    def clear(self) -> None:
        self.calls.clear()


class HttpStub:
    """A local HTTP server answering JSON from canned routes, standing in for Prometheus, Loki or the mapper.

    ``routes`` maps a path to a function of the query parameters that returns the JSON body, or
    ``(status, body)`` for an HTTP error. Every request is recorded in ``requests``.
    """

    def __init__(self) -> None:
        self.routes: dict[str, Any] = {}
        self.requests: list[tuple[str, dict[str, str]]] = []
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server's naming
                url = urllib.parse.urlsplit(self.path)
                query = dict(urllib.parse.parse_qsl(url.query))
                stub.requests.append((url.path, query))
                route = stub.routes.get(url.path)
                status, body = (404, {"error": "no route"}) if route is None else (200, route(query))
                if isinstance(body, tuple):
                    status, body = body
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_: Any) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def matrix(*series: tuple[dict[str, str], list[tuple[float, str]]]) -> dict:
    """A successful Prometheus range-query response with the given series."""
    result = [{"metric": labels, "values": [[t, v] for t, v in values]} for labels, values in series]
    return {"status": "success", "data": {"resultType": "matrix", "result": result}}
