"""Bounded Loki evidence export with explicit completeness information."""

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MAX_LIMIT = 100_000
PAGE_LIMIT = 5000  # Loki's default max_entries_limit_per_query


def _query(url, selector, start_ns, end_ns, limit):
    params = urllib.parse.urlencode(
        {"query": selector, "start": start_ns, "end": end_ns, "limit": limit, "direction": "forward"}
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


def collect_logs(directory, *, url, selector, start, end, limit=5000, max_queries=1024):
    """Split full pages into disjoint Loki windows, with a bounded query budget."""
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"Loki limit must be between 1 and {MAX_LIMIT}")
    if type(max_queries) is not int or not 1 <= max_queries <= MAX_LIMIT:
        raise ValueError(f"Loki max_queries must be between 1 and {MAX_LIMIT}")
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
            page = _query(url, selector, left, right, page_limit)
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
        "limit": limit,
        "rows": rows,
        "possible_truncation": truncated or bool(pending),
        "page_limit": page_limit,
        "queries": queries,
        "max_queries": max_queries,
        "query_budget_exhausted": budget_exhausted or bool(pending),
        "status": "available" if rows else "empty",
    }
    (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary
