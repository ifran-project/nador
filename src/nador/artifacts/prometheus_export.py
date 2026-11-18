"""Export Prometheus query ranges to a flat CSV file."""

from __future__ import annotations

import csv
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_QUERIES = [
    {
        "name": "direct_rtt_mean_ms",
        "query": """
(
  sum by (imsi, ue_ip, slice, probe_role, mode) (
    rate(gtp_teid_latency_observed_ns_sum[30s])
  )
/
  sum by (imsi, ue_ip, slice, probe_role, mode) (
    rate(gtp_teid_latency_observed_ns_count[30s])
  )
) / 1000000
""",
    },
    {
        "name": "direct_rtt_p95_ms",
        "query": """
histogram_quantile(
  0.95,
  sum by (le, imsi, ue_ip, slice, probe_role, mode) (
    rate(gtp_teid_latency_observed_ns_bucket[30s])
  )
) / 1000000
""",
    },
    {
        "name": "direct_median_1s_ms",
        "query": "gtp_teid_latency_median_1s_ns / 1000000",
    },
    {
        "name": "direct_event_rate_hz",
        "query": """
sum by (imsi, ue_ip, slice, probe_role, mode) (
  rate(gtp_teid_latency_observed_ns_count[30s])
)
""",
    },
    {
        "name": "direct_latest_ms",
        "query": "gtp_teid_latency_ns / 1000000",
    },
    {
        "name": "same_packet_upf_rtt_mean_ms",
        "query": """
(
  sum by (imsi, ue_ip, slice, probe_role, mode, direction) (
    rate(gtp_same_packet_upf_rtt_ns_sum[30s])
  )
/
  sum by (imsi, ue_ip, slice, probe_role, mode, direction) (
    rate(gtp_same_packet_upf_rtt_ns_count[30s])
  )
) / 1000000
""",
    },
    {
        "name": "same_packet_gnb_rtt_mean_ms",
        "query": """
(
  sum by (imsi, ue_ip, slice, probe_role, mode, direction) (
    rate(gtp_same_packet_gnb_rtt_ns_sum[30s])
  )
/
  sum by (imsi, ue_ip, slice, probe_role, mode, direction) (
    rate(gtp_same_packet_gnb_rtt_ns_count[30s])
  )
) / 1000000
""",
    },
    {
        "name": "same_packet_gap_mean_ms",
        "query": """
(
  sum by (imsi, ue_ip, slice, probe_role, mode, direction) (
    rate(gtp_same_packet_rtt_gap_ns_sum[30s])
  )
/
  sum by (imsi, ue_ip, slice, probe_role, mode, direction) (
    rate(gtp_same_packet_rtt_gap_ns_count[30s])
  )
) / 1000000
""",
    },
    {
        "name": "same_packet_pair_rate_hz",
        "query": """
sum by (imsi, ue_ip, slice, probe_role, mode, direction) (
  rate(gtp_same_packet_pairs_total[30s])
)
""",
    },
    {
        "name": "lost_observation_events_hz",
        "query": """
sum by (probe_role, stream) (
  rate(gtp_teid_latency_lost_events_total[30s])
)
""",
    },
    {
        "name": "container_cpu_cores",
        "query": """
sum by (namespace, pod, container) (
  rate(container_cpu_usage_seconds_total{container!="POD", image!=""}[30s])
)
""",
    },
    {
        "name": "container_memory_mib",
        "query": """
sum by (namespace, pod, container) (
  container_memory_working_set_bytes{container!="POD", image!=""}
) / 1024 / 1024
""",
    },
]

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
    """Read named PromQL queries from a JSON file, or return the defaults."""
    if not path:
        return list(DEFAULT_QUERIES)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
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
