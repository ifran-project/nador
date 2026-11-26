"""Export Prometheus query ranges to one CSV, then split the samples by timeline window."""

from __future__ import annotations

import csv
import gzip
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import ExitStack
from pathlib import Path

from .timeline import safe_name

logger = logging.getLogger(__name__)

DEFAULT_QUERIES_FILE = Path(__file__).resolve().parents[1] / "assets" / "queries.json"

COMMON_LABELS = [
    "__name__",
    "imsi",
    "ue_ip",
    "slice",
    "probe_role",
    "mode",
    "direction",
    "kind",
    "proto",
    "protocol",
    "reason",
    "teid",
    "ue_rnti",
    "rnti",
    "round",
    "instance",
    "pod",
    "namespace",
    "container",
    "job",
    "stream",
]


def clean_query(query: str) -> str:
    """Collapse whitespace so queries are stored on one line."""
    return " ".join(query.strip().split())


def http_json(url: str, params: dict[str, object] | None = None, timeout: int = 30) -> dict:
    """GET a JSON document, with optional query parameters."""
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise ValueError(f"Expected an HTTP(S) URL, got {url!r}")
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310 - scheme checked above
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def load_queries(path: str | Path | None) -> list[dict[str, str]]:
    """Read named PromQL queries from a JSON file (default: assets/queries.json)."""
    data = json.loads(Path(path or DEFAULT_QUERIES_FILE).read_text(encoding="utf-8"))
    queries = []
    for item in data:
        if not item.get("name") or not item.get("query"):
            raise ValueError(f"Invalid query item: {item!r}")
        queries.append({"name": str(item["name"]), "query": str(item["query"])})
    return queries


def query_range(
    prometheus_url: str,
    query: str,
    start: float,
    end: float,
    step: str,
) -> dict:
    """Run a Prometheus range query."""
    return http_json(
        f"{prometheus_url.rstrip('/')}/api/v1/query_range",
        {
            "query": clean_query(query),
            "start": start,
            "end": end,
            "step": step,
        },
        timeout=60,
    )


def several_cells(prometheus_url: str, start: float, end: float, step: str) -> list[str]:
    """OAI gNB or DU pods (``namespace/service``) that had more than one cell between start and end.

    Their L1 metrics (``oai_gnb_l1_*``) cannot be attributed to a cell: OAI writes none in nrL1_stats.log, and every
    L1 instance of the pod rewrites that file. ``oai_gnb_cells`` is exported by the OAI metrics parser.
    """
    payload = query_range(prometheus_url, "max by (namespace, service) (oai_gnb_cells)", start, end, step)
    pods = set()
    for series in payload.get("data", {}).get("result", []):
        if any(float(value) > 1 for _, value in series.get("values", [])):
            labels = series.get("metric", {})
            pods.add(f"{labels.get('namespace', '')}/{labels.get('service', '')}")
    return sorted(pods)


def write_rows(
    writer: csv.DictWriter,
    query_name: str,
    query: str,
    result: dict,
) -> int:
    """Write one CSV row per sample of a range-query result; return the row count."""
    count = 0
    for series in result.get("data", {}).get("result", []):
        metric = series.get("metric", {})
        labels = {label: metric.get(label, "") for label in COMMON_LABELS}
        for timestamp, value in series.get("values", []):
            row = {
                "query_name": query_name,
                "query": clean_query(query),
                "timestamp": timestamp,
                "value": value,
                "metric_json": json.dumps(metric, sort_keys=True),
            }
            row.update(labels)
            writer.writerow(row)
            count += 1
    return count


def export_ranges(
    directory: Path, *, url: str, start: float, end: float, step: str, queries: list[dict[str, str]]
) -> dict:
    """Write samples and return the same per-query summary saved on disk."""
    out_path = directory / "prometheus_timeseries.csv"
    if end <= start:
        raise ValueError("Prometheus range end must be greater than its start")
    summary = {
        "prometheus_url": url,
        "start": start,
        "end": end,
        "step": step,
        "queries": [],
        "created_at": time.time(),
    }

    fields = ["query_name", "query", "timestamp", "value", "metric_json", *COMMON_LABELS]
    with out_path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=fields)
        writer.writeheader()

        for item in queries:
            name = item["name"]
            query = item["query"]
            query_summary = {"name": name, "query": clean_query(query), "rows": 0}
            try:
                payload = query_range(url, query, start, end, step)
                if payload.get("status") != "success":
                    query_summary["error"] = json.dumps(payload)[:500]
                else:
                    query_summary["rows"] = write_rows(writer, name, query, payload)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                query_summary["error"] = str(exc)
                logger.warning("Prometheus query %r failed: %s", name, exc)
            summary["queries"].append(query_summary)

    (directory / "prometheus_export_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    total_rows = sum(int(item.get("rows", 0)) for item in summary["queries"])
    logger.info("Wrote %d Prometheus samples to %s", total_rows, out_path)
    return summary


# --- Splitting by timeline window ---


def load_windows(path: Path, selected_levels: set[str]) -> list[dict]:
    """Read the timeline windows of the selected levels."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return [row for row in data.get("windows", []) if row.get("level") in selected_levels]


def window_subdir(window: dict) -> Path:
    """Folder of a window under by_window/."""
    return Path(window["level"]) / safe_name(window.get("window_id", "window"))


def to_float(value, default: float = 0.0) -> float:
    """Parse a float, falling back to a default."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def split_windows(directory: Path) -> None:
    """Write one compressed CSV and metadata file per recorded experiment window."""
    full_csv = directory / "prometheus_timeseries.csv"
    timeline_json = directory / "timeline_summary.json"
    out_dir = directory / "by_window"
    out_dir.mkdir(parents=True, exist_ok=True)

    selected_levels = {"campaign", "section"}
    summary_file = out_dir / "split_summary.json"
    for path, kind in ((full_csv, "Prometheus CSV"), (timeline_json, "timeline JSON")):
        if not path.exists():
            error = f"missing {kind}: {path}"
            summary_file.write_text(json.dumps({"error": error, "windows": []}, indent=2), encoding="utf-8")
            logger.warning(error)
            return

    windows = load_windows(timeline_json, selected_levels)
    if not windows:
        summary = {"error": "no matching windows", "windows": []}
        summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        logger.warning(summary["error"])
        return

    writers = []
    summary_windows = []
    with full_csv.open(newline="", encoding="utf-8") as source_fp, ExitStack() as output_files:
        reader = csv.DictReader(source_fp)
        if not reader.fieldnames:
            logger.warning("Empty Prometheus CSV: %s", full_csv)
            return

        for window in windows:
            subdir = out_dir / window_subdir(window)
            subdir.mkdir(parents=True, exist_ok=True)
            csv_path = subdir / "prometheus_timeseries.csv.gz"
            fp = output_files.enter_context(gzip.open(csv_path, "wt", newline="", encoding="utf-8"))
            writer = csv.DictWriter(fp, fieldnames=reader.fieldnames)
            writer.writeheader()
            start = to_float(window.get("start_epoch"))
            end = to_float(window.get("end_epoch"))
            metadata = {
                "window": window,
                "source_csv": str(full_csv),
                "padding_seconds": 0.0,
                "filter_start_epoch": start,
                "filter_end_epoch": end,
                "prometheus_csv": str(csv_path),
                "compressed": True,
                "rows": 0,
            }
            writers.append((start, end, writer, metadata, subdir))

        for row in reader:
            timestamp = to_float(row.get("timestamp"), -1.0)
            for start, end, writer, metadata, _subdir in writers:
                if start <= timestamp <= end:
                    writer.writerow(row)
                    metadata["rows"] += 1

    for _start, _end, _writer, metadata, subdir in writers:
        (subdir / "window_metadata.json").write_text(
            json.dumps(metadata, indent=2),
            encoding="utf-8",
        )
        summary_windows.append(
            {
                "window_id": metadata["window"].get("window_id"),
                "level": metadata["window"].get("level"),
                "name": metadata["window"].get("name"),
                "rows": metadata["rows"],
                "directory": str(subdir),
                "prometheus_csv": metadata["prometheus_csv"],
            }
        )

    summary = {
        "source_csv": str(full_csv),
        "timeline_json": str(timeline_json),
        "window_types": sorted(selected_levels),
        "padding_seconds": 0.0,
        "compressed": True,
        "windows": summary_windows,
    }
    summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    total_rows = sum(item["rows"] for item in summary_windows)
    logger.info("Split %s into %d windows with %d copied rows", full_csv, len(summary_windows), total_rows)
