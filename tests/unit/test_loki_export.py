"""Exporting a run's logs from Loki, page by page, and saying when the export may be incomplete."""

import json
import re

import pytest

from nador.artifacts import ExperimentRun, ran_log_filter
from nador.artifacts.loki_export import collect_logs


def logs_between(lines_per_second):
    """Answer like Loki for a log with lines_per_second lines each second, honouring start, end and limit."""

    def answer(query):
        start, end, limit = int(query["start"]), int(query["end"]), int(query["limit"])
        step = 1_000_000_000 // lines_per_second
        first = -(-start // step) * step
        values = [[str(t), f"line {t}"] for t in range(first, end, step)][:limit]
        return {
            "status": "success",
            "data": {"resultType": "streams", "result": [{"stream": {"pod": "gnb"}, "values": values}]},
        }

    return answer


def exported(directory):
    return [json.loads(line) for line in (directory / "loki.jsonl").read_text().splitlines()]


def test_a_small_range_is_exported_in_one_query(tmp_path, http):
    http.routes["/loki/api/v1/query_range"] = logs_between(2)
    summary = collect_logs(tmp_path, url=http.url, selector="{}", start=0, end=3)
    assert summary | {"rows": 6, "queries": 1, "possible_truncation": False, "status": "available"} == summary
    records = exported(tmp_path)
    assert records[0] == {"timestamp_ns": "0", "labels": {"pod": "gnb"}, "line": "line 0"}
    assert [int(r["timestamp_ns"]) for r in records] == sorted(int(r["timestamp_ns"]) for r in records)


def test_full_pages_are_split_until_every_line_is_exported(tmp_path, http):
    http.routes["/loki/api/v1/query_range"] = logs_between(10)
    summary = collect_logs(tmp_path, url=http.url, selector="{}", start=0, end=4, limit=16)
    assert summary["rows"] == 40
    assert len({r["timestamp_ns"] for r in exported(tmp_path)}) == 40
    assert summary["queries"] > 1
    assert not summary["query_budget_exhausted"]


def test_a_spent_query_budget_is_reported_as_possible_truncation(tmp_path, http):
    http.routes["/loki/api/v1/query_range"] = logs_between(10)
    summary = collect_logs(tmp_path, url=http.url, selector="{}", start=0, end=4, limit=8, max_queries=2)
    assert summary["possible_truncation"]
    assert summary["query_budget_exhausted"]


def test_no_logs_is_reported_as_empty(tmp_path, http):
    http.routes["/loki/api/v1/query_range"] = lambda q: {
        "status": "success",
        "data": {"resultType": "streams", "result": []},
    }
    assert collect_logs(tmp_path, url=http.url, selector="{}", start=0, end=1)["status"] == "empty"


def test_a_loki_error_is_raised_with_its_detail(tmp_path, http):
    http.routes["/loki/api/v1/query_range"] = lambda q: (400, {"error": "parse error at line 1"})
    with pytest.raises(RuntimeError, match="HTTP 400.*parse error"):
        collect_logs(tmp_path, url=http.url, selector="{", start=0, end=1)


def test_an_unexpected_answer_is_refused(tmp_path, http):
    http.routes["/loki/api/v1/query_range"] = lambda q: {"status": "success", "data": {"resultType": "matrix"}}
    with pytest.raises(ValueError, match="did not return successful log streams"):
        collect_logs(tmp_path, url=http.url, selector="{}", start=0, end=1)


def test_only_http_urls_are_queried(tmp_path):
    with pytest.raises(ValueError, match="Expected an HTTP"):
        collect_logs(tmp_path, url="file:///var/log", selector="{}", start=0, end=1)


# Choosing which gNB log lines to leave out

LINES = {
    "ocudu PHY info": "2026-11-24T19:23:13.966694 [PHY     ] [I] [   232.0] PUCCH: rnti=0x4601 format=1",
    "ocudu PHY warning": "2026-11-24T19:23:13.966694 [PHY     ] [W] [   232.0] late slot",
    "ocudu MAC info": "2026-11-24T19:23:13.966694 [MAC     ] [I] [   232.0] DL PDSCH rnti=0x4601",
    "ocudu CU-CP info": "2026-11-24T19:23:13.966694 [CU-CP   ] [I] ue=0 c-rnti=0x4601: UE created",
    "oai NR_PHY info": "[NR_PHY]   I {L1_rx_thread} slot 7",
    "oai NR_MAC warning": "[NR_MAC]   W UE 4601: bad CQI",
    "oai NR_MAC info": "[NR_MAC]   I Frame.Slot 128.0",
    "oai NR_RRC info": "[NR_RRC]   I UE 1 created",
}


def dropped(patterns):
    return sorted(name for name, line in LINES.items() if any(re.search(p, line) for p in patterns))


@pytest.mark.parametrize(
    ("layers", "keep_from", "expected"),
    [
        (["PHY", "MAC"], "warning", ["oai NR_MAC info", "oai NR_PHY info", "ocudu MAC info", "ocudu PHY info"]),
        (["PHY"], "error", ["oai NR_PHY info", "ocudu PHY info", "ocudu PHY warning"]),
        (["cu-cp"], "none", ["ocudu CU-CP info"]),
        (["NR_RRC"], "warning", ["oai NR_RRC info"]),
        (["PHY"], "info", []),
    ],
)
def test_chosen_layers_are_dropped_below_the_level_kept_in_both_log_formats(layers, keep_from, expected):
    assert dropped(ran_log_filter(layers, keep_from=keep_from)) == expected


def test_a_layer_or_level_outside_the_set_is_refused():
    with pytest.raises(ValueError, match="Unknown RAN log layer 'PHYS'; layers: PHY, MAC"):
        ran_log_filter(["PHYS"])
    with pytest.raises(ValueError, match="keep_from must be one of info, warning, error, none"):
        ran_log_filter(["PHY"], keep_from="debug")


def test_counting_lines_needs_an_http_loki():
    from nador.artifacts.loki_export import count_lines  # noqa: PLC0415

    with pytest.raises(ValueError, match="Expected an HTTP"):
        count_lines("file:///etc/passwd", "{}", 0, 1)


def test_the_chosen_lines_are_what_a_run_drops():
    patterns = ran_log_filter(["PHY", "MAC"])
    run = ExperimentRun("unused", loki_url="http://loki:3100", loki_drop=patterns)
    assert run.options.loki_drop == patterns
