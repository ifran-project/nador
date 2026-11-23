"""The script that cuts the CSI file on the gNB node, run with the arguments the collection role gives it."""

import gzip
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "src" / "nador" / "assets" / "roles" / "csi" / "collect" / "files"
T0 = 1767225600  # 2026-01-01 00:00:00 UTC
HEADER = b'# {"format_version": "3.1", "columns": ["frame", "slot", "rnti"]}\nframe,slot,rnti\n'
CSI = HEADER + (
    b"1,0,17921\n"
    b"# TIMESTAMP: 2026-01-01 00:00:05\n2,0,17921\n2,1,17921\n"
    b"# TIMESTAMP: 2026-01-01 00:00:10\n# DROPPED: 3\n3,0,17921\n"
    b"# TIMESTAMP: 2026-01-01 00:00:20\n4,0,17921\n"
    b"5,0,179"  # the gNB is writing this line
)


def load_script():
    spec = importlib.util.spec_from_file_location("csi_tools", SCRIPT / "csi_tools.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def collect(tmp_path, monkeypatch, capsys):
    """Run the script in tmp_path, as ``csi_tools.py collect ...``, and return the summary it prints."""
    monkeypatch.chdir(tmp_path)
    script = load_script()

    def run(*windows, full=True):
        (tmp_path / "csi_per_rb.csv").write_bytes(CSI)
        argv = ["csi_tools.py", "collect", "--src", "csi_per_rb.csv", "--out-dir", "out"]
        if full:
            argv.append("--full")
        if windows:
            (tmp_path / "windows.json").write_text(json.dumps({"windows": list(windows)}))
            argv += ["--windows-json", "windows.json"]
        monkeypatch.setattr(sys, "argv", argv)
        assert script.main() == 0
        return json.loads(capsys.readouterr().out)

    return run


def section(name, start, end):
    return {"level": "section", "window_id": name, "ran_start_epoch": T0 + start, "ran_end_epoch": T0 + end}


def test_the_whole_file_up_to_the_last_complete_line(tmp_path, collect):
    summary = collect()
    assert summary["rows"] == 5
    assert summary["rows_before_first_marker"] == 1
    assert summary["markers"] == 3
    assert summary["dropped_rows"] == 3
    assert summary["incomplete_last_line_bytes"] == len(b"5,0,179")
    assert summary["first_marker_epoch"] == T0 + 5
    assert summary["max_marker_gap_s"] == 10
    assert summary["format_version"] == "3.1"
    assert summary["columns"] == ["frame", "slot", "rnti"]
    assert gzip.decompress((tmp_path / "out" / "csi_per_rb.csv.gz").read_bytes()) == CSI[: CSI.rindex(b"\n") + 1]


def test_each_window_gets_the_batches_acquired_during_it(tmp_path, collect):
    summary = collect(section("idle", 6, 8), section("load", 12, 30), section("later", 40, 50), full=False)
    assert not (tmp_path / "out" / "csi_per_rb.csv.gz").exists()
    assert (summary["windows_written"], summary["windows_empty"]) == (2, 1)
    by_window = tmp_path / "out" / "by_window"
    idle = gzip.decompress((by_window / "section" / "idle" / "csi_per_rb.csv.gz").read_bytes())
    assert idle == HEADER + b"# TIMESTAMP: 2026-01-01 00:00:10\n3,0,17921\n"
    split = json.loads((by_window / "csi_split_summary.json").read_text())
    load = next(w for w in split["windows"] if w["window_id"] == "load")
    assert (load["path"], load["rows"], load["batches"]) == ("by_window/section/load/csi_per_rb.csv.gz", 1, 1)
    assert (load["first_batch_start"], load["last_batch_end"]) == (T0 + 10, T0 + 20)


def test_windows_without_node_times_are_skipped(collect):
    summary = collect({"level": "section", "window_id": "x", "start_epoch": T0}, full=False)
    assert summary["windows_skipped"] == 1


def test_without_a_command_it_shows_its_usage(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["csi_tools.py"])
    assert load_script().main() == 2
    assert "collect" in capsys.readouterr().out
