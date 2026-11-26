"""Scripts NADOR ships into the cluster with its charts: the OCUDU metrics adapter and the cleanup job.

They run inside pods with ``websocket-client`` and ``kubernetes`` installed; here those modules are
replaced by small fakes, so each script's own logic runs as in its pod.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

CHARTS = Path(__file__).resolve().parents[2] / "src" / "nador" / "assets" / "charts"


def load(path, **fakes):
    """Import a chart script with fake third-party modules in place."""
    saved = {name: sys.modules.get(name) for name in fakes}
    sys.modules.update(fakes)
    try:
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        for name, module_before in saved.items():
            if module_before is None:
                sys.modules.pop(name)
            else:
                sys.modules[name] = module_before
    return module


# OCUDU metrics adapter


class Stop(Exception):
    """Ends the adapter's endless reconnect loop in a test."""


@pytest.fixture
def adapter():
    websocket = types.ModuleType("websocket")
    websocket.WebSocketException = type("WebSocketException", (Exception,), {})
    websocket.WebSocketTimeoutException = type("WebSocketTimeoutException", (websocket.WebSocketException,), {})
    return load(CHARTS / "ocudu-metrics" / "files" / "ws_adapter.py", websocket=websocket)


def printed(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


@pytest.mark.parametrize(
    ("message", "forwarded"),
    [
        ('{"cell": {"dl_brate": 1}}', True),
        ('{"cmd": "metrics_subscribe"}', False),  # control acknowledgement
        ('{"timestamp": 1}', False),  # nothing but a timestamp
        ("[1, 2]", False),
    ],
)
def test_only_metric_objects_are_forwarded(adapter, capsys, message, forwarded):
    assert adapter.forward(message) is forwarded
    assert printed(capsys) == ([json.loads(message)] if forwarded else [])


def test_the_c_rnti_of_each_ue_is_forwarded_in_hex_like_the_rnti_mapper(adapter, capsys):
    message = {
        "cells": [
            {
                "cell_metrics": {"pci": 1},
                "ue_list": [{"pci": 1, "rnti": 17921, "cqi": 12}],
                "event_list": [{"slot": 3, "rnti": 17921, "event_type": "ue_add"}],
            }
        ]
    }
    assert adapter.forward(json.dumps(message))
    (cell,) = printed(capsys)[0]["cells"]
    assert cell["ue_list"] == [{"pci": 1, "rnti": "4601", "cqi": 12}]
    assert cell["event_list"][0]["rnti"] == 17921  # a field of the event, left as OCUDU sends it


def test_the_adapter_subscribes_forwards_and_reports_its_health(adapter, capsys, monkeypatch):
    sent = []
    messages = ['{"cell": 1}', "not json", adapter.websocket.WebSocketTimeoutException(), ""]

    class Connection:
        def send(self, text):
            sent.append(json.loads(text))

        def recv(self):
            message = messages.pop(0)
            if isinstance(message, Exception):
                raise message
            return message

        def close(self):
            pass

    connections = [Connection(), OSError("refused")]

    def create_connection(url, **_):
        if not connections:
            raise Stop
        connection = connections.pop(0)
        if isinstance(connection, Exception):
            raise connection
        return connection

    monkeypatch.setenv("WS_URL", "ws://gnb:8001")
    monkeypatch.setattr(adapter.websocket, "create_connection", create_connection, raising=False)
    monkeypatch.setattr(adapter.time, "sleep", lambda _: None)
    with pytest.raises(Stop):
        adapter.run()
    assert sent == [{"cmd": "metrics_subscribe"}]
    output = printed(capsys)
    assert {"cell": 1} in output
    health = [line["internal_"] for line in output if "internal_" in line]
    assert health[-1] | {"connected": 0, "messages_total": 1, "decode_errors_total": 1} == health[-1]


# Cleanup job


def container(name, owner, state):
    return NS(name=name, env=[NS(name="MONITORING_SNIFFER_OWNER", value=owner)]), NS(name=name, state=state)


RUNNING = NS(running=True, terminated=None)
DONE = NS(running=None, terminated=True)
PENDING = NS(running=None, terminated=None)


def pod(*containers):
    specs, statuses = zip(*containers, strict=True) if containers else ((), ())
    return NS(
        metadata=NS(name="amf-0"),
        spec=NS(ephemeral_containers=list(specs)),
        status=NS(ephemeral_container_statuses=list(statuses)),
    )


@pytest.fixture
def cleanup(monkeypatch):
    kubernetes = types.ModuleType("kubernetes")
    stream_module = types.ModuleType("kubernetes.stream")
    execs = []
    stream_module.stream = lambda _api, name, namespace, container, **_: execs.append(container)
    kubernetes.stream = stream_module
    kubernetes.client = NS(exceptions=NS(ApiException=type("ApiException", (Exception,), {"status": 0})))
    module = load(
        CHARTS / "monitoring-controller" / "files" / "cleanup.py",
        kubernetes=kubernetes,
        **{"kubernetes.stream": stream_module},
    )
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    module.execs = execs
    return module


def test_only_this_deployment_s_running_sniffers_are_stopped(cleanup):
    mine = container("amf-sniffer-1", "monitoring", RUNNING)
    other = container("amf-sniffer-2", "someone-else", RUNNING)
    unrelated = container("debugger", "monitoring", RUNNING)
    stopped = container("smf-sniffer-1", "monitoring", DONE)
    api = NS(
        connect_get_namespaced_pod_exec=None,
        read_namespaced_pod=lambda *_: pod(container("amf-sniffer-1", "monitoring", DONE)),
    )
    cleanup.stop_sniffers(api, pod(mine, other, unrelated, stopped), "core", "monitoring")
    assert cleanup.execs == ["amf-sniffer-1"]


def test_a_pending_sniffer_stops_the_cleanup(cleanup):
    with pytest.raises(RuntimeError, match="is pending; cannot confirm safe cleanup"):
        cleanup.stop_sniffers(NS(), pod(container("amf-sniffer-1", "monitoring", PENDING)), "core", "monitoring")


def test_a_sniffer_that_never_stops_is_reported(cleanup, monkeypatch):
    clock = iter(range(0, 100, 10))
    monkeypatch.setattr(cleanup.time, "monotonic", lambda: next(clock))
    sniffer = container("amf-sniffer-1", "monitoring", RUNNING)
    api = NS(connect_get_namespaced_pod_exec=None, read_namespaced_pod=lambda *_: pod(sniffer))
    with pytest.raises(RuntimeError, match=r"Sniffers did not stop: \['amf-sniffer-1'\]"):
        cleanup.stop_sniffers(api, pod(sniffer), "core", "monitoring")


def test_the_controller_is_scaled_down_and_awaited(cleanup, monkeypatch):
    patched, pods = [], [[1], []]
    apps = NS(patch_namespaced_deployment=lambda name, namespace, body: patched.append((name, body)))
    cleanup.kubernetes.client.AppsV1Api = lambda: apps
    monkeypatch.setenv("CLEANUP_TIMEOUT_SECONDS", "60")
    cleanup.stop_controller(NS(list_namespaced_pod=lambda *_, **__: NS(items=pods.pop(0))), "monitoring")
    assert patched == [("monitoring-controller", {"spec": {"replicas": 0}})]


def test_an_absent_controller_is_fine_but_a_stuck_one_is_reported(cleanup, monkeypatch):
    error = cleanup.kubernetes.client.exceptions.ApiException()
    error.status = cleanup.NOT_FOUND

    def missing(*_):
        raise error

    cleanup.kubernetes.client.AppsV1Api = lambda: NS(patch_namespaced_deployment=missing)
    monkeypatch.setenv("CLEANUP_TIMEOUT_SECONDS", "0")
    cleanup.stop_controller(NS(list_namespaced_pod=lambda *_, **__: NS(items=[])), "monitoring")
    with pytest.raises(RuntimeError, match="did not stop before cleanup"):
        cleanup.stop_controller(NS(list_namespaced_pod=lambda *_, **__: NS(items=[1])), "monitoring")
