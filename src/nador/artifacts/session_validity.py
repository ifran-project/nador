"""Conservative, offline session evidence; not a replacement for trial checks.

Sampling cannot exclude transitions between polls. Request intervals bound when
an inventory could have been acquired; they are not exact session event times.
Only allowlisted identity fields are copied into reports.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping
import ipaddress
import json
import math
from pathlib import Path
import re


IDENTITY_FIELDS = (
    "imsi", "ue_ip", "slice_id", "session_key", "ul_teid", "dl_teid",
    "gnb_id", "gnb_n3_ip", "upf_n3_ip", "smf_seid", "upf_seid",
    "smf_pfcp_ip", "upf_pfcp_ip",
)
LIMITATION = (
    "Bounded sampled-inventory evidence only. observed-stable does not exclude "
    "unobserved transitions between or within polls. changed means an identity "
    "different from the baseline was sampled wholly inside the window, not "
    "that the transition time is known. missing means sampled absence, not "
    "proof of session release. No private trial pass criteria are changed."
)
STATUSES = ("observed-stable", "insufficient_coverage", "missing", "changed")


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("non-numeric timestamp")
    if not math.isfinite(value):
        raise ValueError("non-finite timestamp")
    return float(value)


def _identity(item):
    """Normalize mapper identities without copying arbitrary inventory fields."""
    if not isinstance(item, Mapping):
        return None
    result = {}
    try:
        for field in IDENTITY_FIELDS:
            value = item[field]
            if isinstance(value, bool) or value is None or str(value).strip() == "":
                return None
            if field.endswith("_teid"):
                value = value if type(value) is int else int(str(value), 16)
                if not 0 < value <= 0xFFFFFFFF:
                    return None
                value = f"0x{value:08x}"
            elif field.endswith("_seid"):
                value = value if type(value) is int else int(str(value), 10)
                if not 0 < value <= 0xFFFFFFFFFFFFFFFF:
                    return None
                value = str(value)
            elif field.endswith("_ip") or field == "gnb_id":
                value = str(ipaddress.ip_address(value))
            elif not isinstance(value, str):
                return None
            result[field] = value.lower() if field == "slice_id" else value
    except (KeyError, TypeError, ValueError):
        return None
    if result["gnb_id"] != result["gnb_n3_ip"]:
        return None
    return result


def load_history(path):
    """Read JSONL, retaining bad lines as unknown evidence rather than skipping."""
    records = []
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                records.append({"status": "invalid_jsonl"})
    return records


def _prepare(history):
    polls, global_issues = [], []
    previous = None
    run_ids = set()
    for index, record in enumerate(history):
        if not isinstance(record, Mapping):
            global_issues.append("non_object_history_record")
            continue
        try:
            start = _number(record.get("request_started_at"))
            end = _number(record.get("observed_at"))
            if end < start:
                raise ValueError("reversed interval")
        except ValueError:
            global_issues.append("invalid_request_interval")
            continue
        issues = []
        if previous is not None and start < previous["end"]:
            global_issues.append("clock_regression_or_overlapping_requests")
        if previous is not None and end <= previous["end"]:
            global_issues.append("non_increasing_observation_timestamps")
        if "request_duration_seconds" in record:
            try:
                duration = _number(record["request_duration_seconds"])
                if duration < 0 or not math.isclose(end - start, duration, rel_tol=.001, abs_tol=.01):
                    global_issues.append("wall_clock_and_request_duration_disagree")
            except ValueError:
                global_issues.append("invalid_request_duration")
        run_id = record.get("run_id")
        if run_id is not None:
            if not isinstance(run_id, str) or not run_id:
                global_issues.append("invalid_history_run_id")
            else:
                run_ids.add(run_id)
        sequence = record.get("sequence")
        if sequence is not None:
            if type(sequence) is not int or sequence < 0:
                issues.append("invalid_sequence")
            elif previous is not None and type(previous["record"].get("sequence")) is int:
                delta = sequence - previous["record"]["sequence"]
                if delta <= 0:
                    global_issues.append("sequence_regression_or_duplicate")
                elif delta != 1:
                    issues.append("sequence_gap")
        if record.get("status") != "ok":
            issues.append("failed_poll")
        if record.get("possible_truncation") is not False:
            issues.append("truncated_or_unspecified_completeness")
        rows = record.get("ues")
        if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
            issues.append("invalid_inventory")
        else:
            for key in ("count", "reported_count"):
                if key in record and (type(record[key]) is not int or record[key] != len(rows)):
                    issues.append("inventory_count_mismatch")
            if "limit" in record and (type(record["limit"]) is not int
                                      or record["limit"] <= len(rows) or record["limit"] < 1):
                issues.append("inventory_limit_or_truncation")
        poll = dict(index=index, start=start, end=end, record=record, issues=issues)
        polls.append(poll)
        previous = poll
    if len(run_ids) > 1:
        global_issues.append("mixed_history_run_ids")
    if polls:
        for field in ("run_id", "sequence"):
            present = [field in poll["record"] for poll in polls]
            if any(present) and not all(present):
                global_issues.append("inconsistent_" + field + "_metadata")
    return polls, sorted(set(global_issues)), run_ids


def _state(poll, imsi, baseline):
    if poll["issues"]:
        return "unknown", None
    rows = poll["record"]["ues"]
    matches = [row for row in rows if str(row.get("imsi", "")) == imsi]
    if len(matches) > 1:
        return "ambiguous", None
    if not matches:
        if any(not isinstance(row.get("imsi"), str) or not row["imsi"] for row in rows):
            return "partial", None
        return "missing", None
    identity = _identity(matches[0])
    if identity is None:
        return "partial", None
    return ("same" if identity == baseline else "changed"), identity


def _reference(polls, start):
    candidates = [poll for poll in polls if not poll["issues"] and poll["end"] <= start]
    return max(candidates, key=lambda poll: poll["end"], default=None)


def _expected(expected, reference):
    issues = []
    if expected is None:
        if reference is None:
            return {}, ["no_start_bracket_for_baseline"]
        items = reference["record"]["ues"]
        identifiers = [item.get("imsi") for item in items]
        if any(not isinstance(imsi, str) or not imsi for imsi in identifiers):
            issues.append("partial_inferred_baseline")
        if len(set(map(str, identifiers))) != len(identifiers):
            issues.append("ambiguous_inferred_baseline")
        expected = {str(item.get("imsi", index)): item for index, item in enumerate(items)}
    elif isinstance(expected, list):
        expected = {str(index): item for index, item in enumerate(expected)}
    if not isinstance(expected, Mapping):
        return {}, ["invalid_expected_identities"]
    result = {}
    for name, item in expected.items():
        identity = _identity(item)
        result[str(name)] = identity
        if identity is None:
            issues.append("incomplete_expected_identity")
    imsis = [item["imsi"] for item in result.values() if item is not None]
    if len(imsis) != len(set(imsis)):
        issues.append("ambiguous_expected_imsi")
    if not result:
        issues.append("no_expected_identities")
    return result, issues


def _summary_status(statuses):
    return max(statuses, key=STATUSES.index, default="insufficient_coverage")


def _poll_ref(poll):
    return {"record_index": poll["index"], "request_started_at": poll["start"],
            "observed_at": poll["end"]}


def validate_session_windows(history: Iterable[dict], windows: list[dict],
                             expected_identities=None, max_gap_seconds=10):
    """Return per-window/per-UE bounded evidence, preserving input poll order.

    history contains decoded JSONL records (or is a JSONL path). Windows use
    start/end numeric epochs, optionally name and run_id; start_epoch/end_epoch
    are also accepted. Every expected UE is checked in every selected window.
    Expected identities are a name-to-full-mapping dict or list of mappings.
    With None, baseline identities come from the last successful pre-workload
    bracket of the earliest window, not list position or a later live lookup.

    Coverage requires clean polls fully before/after the workload, complete UE
    identity at every intervening poll, and bounded worst-case acquisition
    separation (next observed_at - previous request_started_at). Errors,
    truncation, partial/duplicate identities, boundary mismatches, request
    duration, and time/sequence anomalies remain explicit unknowns. Positive
    in-window changed/missing evidence is retained even if coverage is unknown.
    """
    gap_limit = _number(max_gap_seconds)
    if gap_limit <= 0:
        raise ValueError("max_gap_seconds must be finite and positive")
    if isinstance(history, (str, Path)):
        history = load_history(history)
    polls, global_issues, run_ids = _prepare(history)
    normalized = []
    for index, window in enumerate(windows):
        if not isinstance(window, Mapping):
            global_issues.append("invalid_window")
            continue
        try:
            start = _number(window.get("start", window.get("start_epoch")))
            end = _number(window.get("end", window.get("end_epoch")))
            if end <= start:
                raise ValueError("nonpositive window")
        except ValueError:
            global_issues.append("invalid_window_interval")
            continue
        if window.get("run_id") is not None and run_ids != {window["run_id"]}:
            global_issues.append("timeline_history_run_id_mismatch")
        normalized.append({"name": str(window.get("name", index)), "start": start, "end": end})
    first = min((window["start"] for window in normalized), default=None)
    reference = _reference(polls, first) if first is not None else None
    expected, baseline_issues = _expected(expected_identities, reference)
    global_issues += baseline_issues
    if not normalized:
        global_issues.append("no_traffic_windows")
    report = {"schema_version": 1, "status": "insufficient_coverage", "limitation": LIMITATION,
              "max_gap_seconds": gap_limit, "identity_fields": list(IDENTITY_FIELDS),
              "expected_source": "supplied" if expected_identities is not None else "earliest_start_bracket",
              "population_scope": "all expected UEs in every selected window; inferred populations may be incomplete",
              "issues": sorted(set(global_issues)), "windows": []}
    for window in normalized:
        start, end = window["start"], window["end"]
        left = _reference(polls, start)
        right = min((poll for poll in polls if not poll["issues"] and poll["start"] >= end),
                    key=lambda poll: poll["start"], default=None)
        issues = list(report["issues"])
        if left is None:
            issues.append("no_successful_start_bracket")
        if right is None:
            issues.append("no_successful_end_bracket")
        low = left["start"] if left else start
        high = right["end"] if right else end
        relevant = [poll for poll in polls if poll["end"] >= low and poll["start"] <= high]
        bad_polls = [{**_poll_ref(poll), "issues": poll["issues"]}
                     for poll in relevant if poll["issues"]]
        if bad_polls:
            issues.append("unknown_inventory_polls")
        for poll in relevant:
            if poll["end"] - poll["start"] > gap_limit:
                issues.append("request_interval_exceeds_max_gap")
        gaps = []
        for before, after in zip(relevant, relevant[1:]):
            separation = after["end"] - before["start"]
            if separation > gap_limit:
                gaps.append({"from_record": before["index"], "to_record": after["index"],
                             "worst_case_seconds": separation})
        if gaps:
            issues.append("acquisition_gap_exceeds_max_gap")
        if left and start - left["start"] > gap_limit:
            issues.append("start_bracket_too_distant")
        if right and right["end"] - end > gap_limit:
            issues.append("end_bracket_too_distant")
        ues = {}
        for name, baseline in expected.items():
            ue_issues, evidence, later = list(issues), [], []
            during = []
            previous_state = None
            previous_poll = None
            if baseline is None:
                ue_issues.append("incomplete_baseline")
            else:
                for poll in polls:
                    state, identity = _state(poll, baseline["imsi"], baseline)
                    inside = start <= poll["start"] and poll["end"] <= end
                    selected = poll in relevant
                    if selected and state in ("unknown", "partial", "ambiguous"):
                        ue_issues.append("unknown_or_ambiguous_ue_identity")
                    if state not in ("same", "unknown"):
                        event = {**_poll_ref(poll), "kind": state}
                        if identity is not None:
                            event["identity"] = identity
                        if inside:
                            event["timing"] = "observed_during_window"
                            if state in ("changed", "missing"):
                                during.append(state)
                        elif poll["start"] >= end:
                            event["timing"] = "observed_after_window"
                        elif poll["end"] <= start:
                            event["timing"] = "observed_before_window"
                        else:
                            event["timing"] = "boundary_straddling_request"
                        if state in ("changed", "missing") and previous_state == "same":
                            event["transition_bounds"] = [previous_poll["start"], poll["end"]]
                            event["transition_proven_after_window"] = previous_poll["start"] >= end
                        if selected:
                            evidence.append(event)
                            if not inside:
                                ue_issues.append("boundary_identity_not_baseline")
                        elif poll["start"] >= end:
                            later.append(event)
                    previous_state, previous_poll = state, poll
            coverage = not ue_issues
            status = _summary_status(during) if during else (
                "observed-stable" if coverage else "insufficient_coverage")
            # Clock/run-order anomalies prevent even a positive observation from
            # being attributed confidently to this workload's time interval.
            if report["issues"]:
                status = "insufficient_coverage"
            ues[name] = {"status": status, "coverage_complete": coverage,
                         "baseline": baseline, "issues": sorted(set(ue_issues)),
                         "evidence": evidence, "after_window_evidence": later}
        result = {**window, "status": _summary_status([ue["status"] for ue in ues.values()]),
                  "coverage_complete": bool(ues) and all(ue["coverage_complete"] for ue in ues.values()),
                  "issues": sorted(set(issues)), "unknown_polls": bad_polls, "gaps": gaps,
                  "start_bracket": _poll_ref(left) if left else None,
                  "end_bracket": _poll_ref(right) if right else None, "ues": ues}
        report["windows"].append(result)
    report["status"] = _summary_status([window["status"] for window in report["windows"]])
    return report


def extract_traffic_windows(timeline, names=None):
    """Select ue..._uplink/downlink sections, or exactly requested names."""
    if not isinstance(timeline, Mapping) or not isinstance(timeline.get("windows"), list):
        raise ValueError("invalid timeline")
    requested = set(names) if names is not None else None
    windows = []
    for row in timeline["windows"]:
        if not isinstance(row, Mapping):
            raise ValueError("invalid timeline window")
        name = row.get("name", "")
        selected = name in requested if requested is not None else (
            row.get("level") == "section" and isinstance(name, str)
            and re.fullmatch(r"ue[A-Za-z0-9_.-]*[_-](uplink|downlink)", name) is not None)
        if selected:
            windows.append({"name": name, "start": row.get("start_epoch"),
                            "end": row.get("end_epoch"), **(
                                {"run_id": timeline["run_id"]} if "run_id" in timeline else {})})
    if requested is not None and requested - {row["name"] for row in windows}:
        raise ValueError("requested window missing")
    for key in ("unmatched_starts", "unmatched_ends"):
        for event in timeline.get(key, []):
            name = event.get("name", "")
            if (requested is not None and name in requested) or (
                    requested is None and re.fullmatch(r"ue[A-Za-z0-9_.-]*[_-](uplink|downlink)", name)):
                raise ValueError("unpaired traffic window")
    return windows


def build_report(artifact_directory, max_gap_seconds=10, *, expected_identities=None,
                 window_names=None, output_directory=None):
    """Write session_window_validity.json and return it, including input failures.

    Intended integration: call after the sampler has been fetched and the
    timeline generated. This helper neither runs live queries nor changes any
    ObservabilityRun/private-runner status. output_directory supports offline
    analysis without writing into the source artifact directory.
    """
    directory = Path(artifact_directory)
    try:
        timeline = json.loads((directory / "timeline_summary.json").read_text(encoding="utf-8"))
        windows = extract_traffic_windows(timeline, window_names)
        history = load_history(directory / "ue_context" / "history.jsonl")
        report = validate_session_windows(history, windows, expected_identities, max_gap_seconds)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        report = {"schema_version": 1, "status": "insufficient_coverage", "limitation": LIMITATION,
                  "issues": ["missing_or_invalid_artifact_or_arguments"], "windows": []}
    report["source_apis"] = {
        "inventory": "/inventory/ues?limit=<configured-limit> (sampled inventory, not an event ledger)",
        "history": "ue_context/history.jsonl",
        "timeline": "timeline_summary.json",
    }
    try:
        destination = Path(output_directory) if output_directory is not None else directory
        (destination / "session_window_validity.json").write_text(
            json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    except (OSError, ValueError, TypeError):
        report["status"] = "insufficient_coverage"
        report["issues"].append("report_write_failed")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact_directory", type=Path)
    parser.add_argument("--max-gap-seconds", type=float, default=10)
    parser.add_argument("--expected-mapping", type=Path, help="JSON name-to-full-identity mapping")
    parser.add_argument("--window", action="append", help="Exact window name; repeat to select several")
    parser.add_argument("--output-directory", type=Path)
    args = parser.parse_args(argv)
    expected = None
    if args.expected_mapping is not None:
        try:
            expected = json.loads(args.expected_mapping.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # An explicitly supplied but unreadable baseline must not fall back
            # to an inferred baseline and appear to pass.
            expected = {}
    report = build_report(args.artifact_directory, args.max_gap_seconds,
                          expected_identities=expected, window_names=args.window,
                          output_directory=args.output_directory)
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0 if report["status"] == "observed-stable" else 1


if __name__ == "__main__":
    raise SystemExit(main())
