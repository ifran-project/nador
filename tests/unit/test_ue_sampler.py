"""The UE sampler: polling the mapper's inventory during a run and keeping an honest history."""

import json
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from nador.artifacts import ue_sampler

UE = {"imsi": "001010000000001", "ue_ip": "10.45.0.2", "internal": "dropped"}


def options(tmp_path, **given):
    defaults = {
        "out_dir": str(tmp_path / "out"),
        "stop_file": str(tmp_path / "STOP"),
        "run_id": "r1",
        "interval_seconds": 0.01,
        "max_seconds": 60,
        "limit": 10,
        "url": "",
        "namespace": "monitoring",
        "service": "ue-mapper-api",
        "service_port": 8080,
        "timeout_seconds": 5,
    }
    return SimpleNamespace(**defaults | given)


def history(args):
    return [json.loads(line) for line in (ue_sampler.Path(args.out_dir) / "history.jsonl").read_text().splitlines()]


def stop_after(count, answers):
    """A fetch that returns answers in turn and asks the sampler to stop after count calls."""
    stop = threading.Event()
    calls = []

    def fetch():
        calls.append(1)
        if len(calls) >= count:
            stop.set()
        answer = answers[min(len(calls), len(answers)) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return fetch, stop


# Inventory records


def test_only_known_ue_fields_are_kept():
    record = ue_sampler.inventory_record({"ues": [UE], "count": 1, "limit": 10}, limit=10)
    assert record["ues"] == [{"imsi": "001010000000001", "ue_ip": "10.45.0.2"}]
    assert record["possible_truncation"] is False


def test_a_full_page_may_be_truncated():
    rows = [UE, UE]
    assert ue_sampler.inventory_record({"ues": rows, "count": 5}, limit=2)["possible_truncation"] is True


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "Expected an inventory object"),
        ({"ues": "x"}, "Expected an inventory object"),
        ({"ues": ["x"]}, "non-object UE row"),
        ({"ues": [], "count": -1}, "Invalid inventory count or limit"),
        ({"ues": [], "limit": 0}, "Invalid inventory count or limit"),
        ({"ues": [], "count": True}, "Invalid inventory count or limit"),
    ],
)
def test_a_malformed_inventory_is_refused(payload, message):
    with pytest.raises(ValueError, match=message):
        ue_sampler.inventory_record(payload, limit=10)


# Sampling


def test_sampling_writes_history_a_ready_file_and_a_summary(tmp_path):
    args = options(tmp_path)
    fetch, stop = stop_after(2, [{"ues": [UE]}])
    summary = ue_sampler.sample(args, fetch, stop)
    assert summary | {"samples": 3, "errors": 0, "stop_reason": "requested"} == summary
    records = history(args)
    assert [r["sequence"] for r in records] == [0, 1, 2]
    assert [r["final"] for r in records] == [False, False, True]  # one last sample once stop is asked
    assert (tmp_path / "out" / "ready.json").exists()
    assert json.loads((tmp_path / "out" / "summary.json").read_text())["samples"] == 3


def test_a_failed_request_is_recorded_as_an_error_not_as_no_ues(tmp_path):
    args = options(tmp_path)
    fetch, stop = stop_after(2, [OSError("connection refused"), {"ues": []}])
    summary = ue_sampler.sample(args, fetch, stop)
    first, second, _final = history(args)
    assert first["status"] == "error"
    assert "connection refused" in first["error"]
    assert second["status"] == "ok"
    assert summary["errors"] == 1


def test_the_stop_file_ends_sampling(tmp_path):
    args = options(tmp_path)
    (tmp_path / "STOP").touch()
    summary = ue_sampler.sample(args, lambda: {"ues": []}, threading.Event())
    assert summary["samples"] == 1
    assert summary["stop_reason"] == "requested"


def test_sampling_ends_at_max_seconds(tmp_path):
    args = options(tmp_path, max_seconds=0)
    summary = ue_sampler.sample(args, lambda: {"ues": []}, threading.Event())
    assert summary["stop_reason"] == "max_seconds"


def test_a_previous_history_is_never_overwritten(tmp_path):
    args = options(tmp_path)
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "history.jsonl").write_text("kept\n")
    with pytest.raises(FileExistsError):
        ue_sampler.sample(args, lambda: {"ues": []}, threading.Event())


# Reaching the mapper


def test_the_mapper_is_read_directly_when_a_url_is_given(tmp_path, http):
    http.routes["/inventory/ues"] = lambda q: {"ues": [UE], "count": 1, "limit": int(q["limit"])}
    args = options(tmp_path, url=http.url + "/", limit=7)
    assert ue_sampler.fetch_inventory(args, [])["limit"] == 7


def test_otherwise_the_mapper_is_read_through_the_kubernetes_service_proxy(tmp_path, monkeypatch):
    calls = []

    def kubectl(argv, **_):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"ues": []}), stderr="")

    monkeypatch.setattr(ue_sampler.subprocess, "run", kubectl)
    assert ue_sampler.fetch_inventory(options(tmp_path), ["--kubeconfig", "k"]) == {"ues": []}
    assert calls[0][:3] == ["kubectl", "--kubeconfig", "k"]
    assert calls[0][-1] == "/api/v1/namespaces/monitoring/services/http:ue-mapper-api:8080/proxy/inventory/ues?limit=10"


def test_a_failing_service_proxy_raises_its_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ue_sampler.subprocess, "run", lambda argv, **_: subprocess.CompletedProcess(argv, 1, "", "forbidden")
    )
    with pytest.raises(RuntimeError, match="forbidden"):
        ue_sampler.fetch_inventory(options(tmp_path), [])


def test_the_kubeconfig_is_the_requested_one_or_the_first_readable(tmp_path, monkeypatch):
    assert ue_sampler.kubeconfig_args("/etc/k.conf") == ["--kubeconfig", "/etc/k.conf"]
    monkeypatch.setenv("KUBECONFIG", "/x")
    assert ue_sampler.kubeconfig_args("auto") == []
    monkeypatch.delenv("KUBECONFIG")
    monkeypatch.setattr(ue_sampler.Path, "home", lambda: tmp_path)
    (tmp_path / ".kube").mkdir()
    (tmp_path / ".kube" / "config").write_text("")
    assert ue_sampler.kubeconfig_args("auto") == ["--kubeconfig", str(tmp_path / ".kube" / "config")]


# Command line


def test_the_command_samples_and_prints_its_summary(tmp_path, monkeypatch, capsys, http):
    http.routes["/inventory/ues"] = lambda q: {"ues": [UE]}
    (tmp_path / "STOP").touch()
    argv = ["ue-sampler", "--out-dir", str(tmp_path / "out"), "--stop-file", str(tmp_path / "STOP")]
    monkeypatch.setattr(sys, "argv", [*argv, "--run-id", "r1", "--url", http.url])
    monkeypatch.setattr(ue_sampler.signal, "signal", lambda *_: None)
    assert ue_sampler.main() == 0
    assert json.loads(capsys.readouterr().out)["samples"] == 1


@pytest.mark.parametrize("bad", [["--interval-seconds", "0"], ["--timeout-seconds", "61"], ["--service-port", "0"]])
def test_the_command_refuses_out_of_range_options(tmp_path, monkeypatch, bad):
    argv = ["ue-sampler", "--out-dir", "o", "--stop-file", "s", "--run-id", "r", *bad]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit):
        ue_sampler.main()
