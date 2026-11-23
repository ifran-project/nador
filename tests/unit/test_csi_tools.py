"""The script that cuts the CSI file on the gNB node, run as the collection role runs it."""

import gzip
import json
import subprocess
import sys
from pathlib import Path

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


def collect(tmp_path, *windows, full=True):
    source = tmp_path / "csi_per_rb.csv"
    source.write_bytes(CSI)
    command = [sys.executable, str(SCRIPT / "csi_tools.py"), "collect", "--src", str(source), "--out-dir", "out"]
    if full:
        command.append("--full")
    if windows:
        (tmp_path / "windows.json").write_text(json.dumps({"windows": list(windows)}))
        command += ["--windows-json", "windows.json"]
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, check=True)  # noqa: S603 - our script
    return json.loads(result.stdout)


def section(name, start, end):
    return {"level": "section", "window_id": name, "ran_start_epoch": T0 + start, "ran_end_epoch": T0 + end}


def test_the_whole_file_up_to_the_last_complete_line(tmp_path):
    summary = collect(tmp_path)
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


def test_each_window_gets_the_batches_acquired_during_it(tmp_path):
    summary = collect(tmp_path, section("idle", 6, 8), section("load", 12, 30), section("later", 40, 50), full=False)
    assert not (tmp_path / "out" / "csi_per_rb.csv.gz").exists()
    assert (summary["windows_written"], summary["windows_empty"]) == (2, 1)
    by_window = tmp_path / "out" / "by_window"
    idle = gzip.decompress((by_window / "section" / "idle" / "csi_per_rb.csv.gz").read_bytes())
    assert idle == HEADER + b"# TIMESTAMP: 2026-01-01 00:00:10\n3,0,17921\n"
    split = json.loads((by_window / "csi_split_summary.json").read_text())
    load = next(w for w in split["windows"] if w["window_id"] == "load")
    assert (load["path"], load["rows"], load["batches"]) == ("by_window/section/load/csi_per_rb.csv.gz", 1, 1)
    assert (load["first_batch_start"], load["last_batch_end"]) == (T0 + 10, T0 + 20)


def test_windows_without_node_times_are_skipped(tmp_path):
    summary = collect(tmp_path, {"level": "section", "window_id": "x", "start_epoch": T0}, full=False)
    assert summary["windows_skipped"] == 1
