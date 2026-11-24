"""The throughput summary of the iperf3 reports a run saved."""

import json

from nador.artifacts import iperf_summary


def report(sent, received=None, retransmits=3):
    end = {"sum_sent": {"bits_per_second": sent * 1e6, "retransmits": retransmits}}
    if received is not None:
        end["sum_received"] = {"bits_per_second": received * 1e6}
    return {"start": {}, "end": end}


def save(directory, name, data):
    (directory / f"{name}.json").write_text(json.dumps(data))


def test_one_line_per_step_in_the_scenario_order(tmp_path):
    save(tmp_path, "uplink", report(20, 19.5))
    save(tmp_path, "downlink", report(100, 98.25, retransmits=0))
    save(tmp_path, "extra", report(1))
    save(tmp_path, "scenario", [{"name": "downlink"}, {"name": "uplink"}])
    save(tmp_path, "experiment_metadata", {"status": "ok"})
    assert iperf_summary(tmp_path).splitlines() == [
        "downlink  sent   100.0 Mbps  received    98.2 Mbps  retransmits 0",
        "uplink    sent    20.0 Mbps  received    19.5 Mbps  retransmits 3",
        "extra     sent     1.0 Mbps  received       - Mbps  retransmits 3",
    ]


def test_a_failed_step_shows_its_error(tmp_path):
    save(tmp_path, "uplink", {"error": "iperf3 left no report: timeout"})
    assert iperf_summary(tmp_path) == "uplink  ERROR iperf3 left no report: timeout"


def test_a_run_without_iperf(tmp_path):
    (tmp_path / "notes.json").write_text("not json")
    assert iperf_summary(tmp_path) == f"No iperf report in {tmp_path}"
