"""Setup snapshots: what a host or the cluster looked like, without archiving secrets."""

import json
import subprocess

import pytest

from nador.artifacts import setup_snapshot

POD = {
    "metadata": {
        "namespace": "ran",
        "name": "gnb-0",
        "uid": "u1",
        "labels": {"app": "oai-gnb"},
        "annotations": {"k8s.v1.cni.cncf.io/networks": "n3"},
    },
    "spec": {
        "nodeName": "node1",
        "containers": [{"name": "gnb", "image": "oai/gnb:1"}],
        "volumes": [{"configMap": {"name": "gnb-config"}}, {"emptyDir": {}}],
    },
    "status": {"podIP": "10.0.0.5", "phase": "Running", "containerStatuses": [{"name": "gnb", "ready": True}]},
}
CLUSTER = {
    "version": {"clientVersion": {"gitVersion": "v1.30"}},
    "nodes": {"items": [{"metadata": {"name": "node1", "uid": "n1"}, "status": {"capacity": {"cpu": "8"}}}]},
    "pods": {"items": [POD]},
    "services": {"items": [{"metadata": {"name": "amf", "namespace": "core"}, "spec": {"ports": [{"port": 80}]}}]},
    "configmaps": {"items": [{"metadata": {"name": "c", "namespace": "core"}, "data": {"password": "secret"}}]},
}


def fake_commands(monkeypatch, answers):
    """Replace subprocess.run: answers maps a command keyword to (returncode, stdout) or an exception."""
    calls = []

    def run(argv, **_):
        calls.append(argv)
        key = next((word for word in answers if word in argv), None)
        answer = answers.get(key, (0, ""))
        if isinstance(answer, Exception):
            raise answer
        code, out = answer
        return subprocess.CompletedProcess(
            argv, code, stdout=out if isinstance(out, str) else json.dumps(out), stderr="err" if code else ""
        )

    monkeypatch.setattr(setup_snapshot.subprocess, "run", run)
    return calls


def snapshot(capsys, *argv):
    setup_snapshot.main(list(argv))
    return json.loads(capsys.readouterr().out)


def test_a_host_snapshot_records_each_tool_and_its_result(monkeypatch, capsys):
    fake_commands(
        monkeypatch,
        {"lscpu": (0, {"lscpu": []}), "free": (0, "Mem: 1"), "timedatectl": FileNotFoundError("no timedatectl")},
    )
    report = snapshot(capsys, "--mode", "host")
    data = report["data"]
    assert report["mode"] == "host"
    assert data["cpu"]["data"] == {"lscpu": []}  # JSON output is parsed
    assert data["memory"]["data"] == "Mem: 1"  # text output is kept as text
    assert data["clock"]["status"] == "error"
    assert "no timedatectl" in data["clock"]["error"]


def test_a_cluster_snapshot_summarises_nodes_pods_and_services(monkeypatch, capsys):
    fake_commands(monkeypatch, {word: (0, CLUSTER[word]) for word in CLUSTER})
    data = snapshot(capsys, "--mode", "cluster", "--kubeconfig", "/etc/k.conf")["data"]
    assert data["nodes"]["data"][0]["capacity"] == {"cpu": "8"}
    (pod,) = data["pods"]["data"]
    assert (
        pod | {"node": "node1", "pod_ip": "10.0.0.5", "configmaps": ["gnb-config"], "networks_requested": "n3"} == pod
    )
    assert pod["containers"] == [
        {"kind": "containers", "name": "gnb", "image": "oai/gnb:1", "resources": {}, "ports": [], "volume_mounts": []}
    ]
    assert data["services"]["data"][0]["name"] == "amf"


def test_configmaps_are_fingerprinted_never_copied(monkeypatch, capsys):
    fake_commands(monkeypatch, {word: (0, CLUSTER[word]) for word in CLUSTER})
    text = json.dumps(snapshot(capsys, "--mode", "cluster", "--kubeconfig", "/k")["data"])
    assert "secret" not in text
    (fingerprint,) = json.loads(text)["configmap_fingerprints"]["data"]
    assert len(fingerprint["sha256"]) == 64


def test_a_failing_kubectl_is_recorded_not_raised(monkeypatch, capsys):
    fake_commands(monkeypatch, {"kubectl": (1, "")})
    data = snapshot(capsys, "--mode", "cluster", "--kubeconfig", "/k")["data"]
    assert {data[key]["status"] for key in ("nodes", "pods", "services", "configmap_fingerprints")} == {"error"}


def test_a_hanging_command_times_out(monkeypatch, capsys):
    fake_commands(monkeypatch, {"lscpu": subprocess.TimeoutExpired("lscpu", 15)})
    assert snapshot(capsys, "--mode", "host")["data"]["cpu"]["status"] == "error"


def test_the_kubeconfig_is_found_when_not_given(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.setattr(setup_snapshot.Path, "home", lambda: tmp_path)
    (tmp_path / ".kube").mkdir()
    (tmp_path / ".kube" / "config").write_text("")
    calls = fake_commands(monkeypatch, {"kubectl": (0, {"items": []})})
    snapshot(capsys, "--mode", "cluster")
    assert calls[0][:4] == ["kubectl", "--request-timeout=15s", "--kubeconfig", str(tmp_path / ".kube" / "config")]


def test_the_mode_is_required():
    with pytest.raises(SystemExit):
        setup_snapshot.main([])
