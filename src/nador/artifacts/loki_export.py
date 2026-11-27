"""Bounded Loki evidence export with explicit completeness information."""

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PAGE_LIMIT = 5000  # Loki's default max_entries_limit_per_query
# OCUDU's PHY at info/debug level writes a line per slot ("[PHY     ] [I] PUCCH: rnti=..."): hundreds of thousands
# of lines a minute. Warnings and errors ("[W]", "[E]") are kept. OAI logs "[PHY]   I": not matched.
OCUDU_PHY_DETAIL = r"\[PHY *\] \[[DI]\]"


# gNB/CU/DU log layers, as OCUDU ("[PHY     ] [I]") and OAI ("[NR_PHY]   I") name them; OAI's NR_ prefix is implied.
RAN_LOG_LAYERS = (
    "PHY", "MAC", "SCHED", "RLC", "PDCP", "SDAP", "GTPU", "RRC", "NGAP", "F1AP", "E1AP",
    "CU-CP", "CU-UP", "DU", "FAPI", "RU", "OFH", "HW", "SCTP", "METRICS",
)  # fmt: skip
# Level letters below each threshold: OCUDU writes D/I/W/E, OAI also T (trace) and A (analysis, kept with errors).
_DROPPED_BELOW = {"info": "DT", "warning": "DTI", "error": "DTIW", "none": "DTIWEA"}


def ran_log_filter(layers, keep_from="warning"):
    """Patterns for ``loki_drop`` leaving out the gNB/CU/DU log lines of ``layers`` below ``keep_from``.

    ``layers`` are names from ``RAN_LOG_LAYERS`` (PHY, MAC, RLC, RRC, NGAP...), matched in OCUDU and OAI logs.
    ``keep_from`` is the lowest level kept: ``info`` (drops debug), ``warning`` (drops debug and info),
    ``error``, or ``none`` (drops the layer entirely). Lines of other layers and other components are untouched.
    """
    if keep_from not in _DROPPED_BELOW:
        raise ValueError(f"keep_from must be one of {', '.join(_DROPPED_BELOW)}, not {keep_from!r}")
    letters = _DROPPED_BELOW[keep_from]
    patterns = []
    for layer in layers:
        name = layer.upper().removeprefix("NR_")
        if name not in RAN_LOG_LAYERS:
            raise ValueError(f"Unknown RAN log layer {layer!r}; layers: {', '.join(RAN_LOG_LAYERS)}")
        patterns.append(rf"\[{name} *\] \[[{letters}]\]|\[(NR_)?{name}\] +[{letters}] ")
    return patterns


def log_query(selector, drop=()):
    """The LogQL query: the stream selector, then a line filter dropping each pattern (RE2) in Loki itself."""
    query = selector
    for pattern in drop:
        if not pattern or "`" in pattern:
            raise ValueError(f"Log line patterns must be non-empty and without backquotes: {pattern!r}")
        query += f" !~ `{pattern}`"
    return query


def count_lines(url, query, start, end):
    """How many lines the query returns between start and end (s), asked to Loki; None when it cannot say."""
    seconds = max(1, math.ceil(end - start))
    params = urllib.parse.urlencode(
        {"query": f"sum(count_over_time({query} [{seconds}s]))", "time": int(math.ceil(end) * 1e9)}
    )
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise ValueError(f"Expected an HTTP(S) Loki URL, got {url!r}")
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/loki/api/v1/query?" + params, timeout=60) as response:  # noqa: S310
            result = json.load(response).get("data", {}).get("result", [])
    except (OSError, ValueError):  # an estimate only: the export itself reports real failures
        return None
    return int(float(result[0]["value"][1])) if result else 0


def query_logs(url, selector, start_ns, end_ns, limit, *, direction="forward"):
    """Log lines of a LogQL selector between two times (ns), oldest first; ``limit`` from the ``direction`` end."""
    params = urllib.parse.urlencode(
        {"query": selector, "start": start_ns, "end": end_ns, "limit": limit, "direction": direction}
    )
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise ValueError(f"Expected an HTTP(S) Loki URL, got {url!r}")
    endpoint = url.rstrip("/") + "/loki/api/v1/query_range?" + params
    try:
        with urllib.request.urlopen(endpoint, timeout=30) as response:  # noqa: S310 - scheme checked above
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        with error:
            detail = error.read(1024).decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Loki query returned HTTP {error.code}: {detail or error.reason}") from error
    if payload.get("status") != "success" or payload.get("data", {}).get("resultType") != "streams":
        raise ValueError("Loki did not return successful log streams")
    records = [
        {"timestamp_ns": str(value[0]), "labels": stream["stream"], "line": value[1]}
        for stream in payload["data"]["result"]
        for value in stream["values"]
    ]
    records.sort(key=lambda row: int(row["timestamp_ns"]))
    return records


def collect_logs(directory, *, url, selector, start, end, limit=5000, max_queries=1024, drop=()):
    """Split full pages into disjoint Loki windows, with a bounded query budget, leaving out ``drop`` lines."""
    query = log_query(selector, drop)
    began = time.monotonic()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    page_limit = min(limit, PAGE_LIMIT)
    pending = [(int(start * 1e9), int(end * 1e9))]
    rows = 0
    truncated = False
    budget_exhausted = False
    queries = 0
    with (directory / "loki.jsonl").open("w", encoding="utf-8") as output:
        while pending and queries < max_queries:
            left, right = pending.pop()
            page = query_logs(url, query, left, right, page_limit)
            queries += 1
            if len(page) >= page_limit and right - left > 1:
                # Reserve a query for every pending window before subdividing.
                if queries + len(pending) + 2 <= max_queries:
                    middle = (left + right) // 2
                    pending.extend(((middle, right), (left, middle)))
                    continue
                budget_exhausted = True
            for record in page:
                output.write(json.dumps(record) + "\n")
            rows += len(page)
            truncated |= len(page) >= page_limit
    summary = {
        "start": start,
        "end": end,
        "selector": selector,
        "dropped_lines_matching": list(drop),
        "limit": limit,
        "rows": rows,
        "possible_truncation": truncated or bool(pending),
        "page_limit": page_limit,
        "queries": queries,
        "max_queries": max_queries,
        "query_budget_exhausted": budget_exhausted or bool(pending),
        "status": "available" if rows else "empty",
        "seconds": round(time.monotonic() - began, 1),
    }
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary
