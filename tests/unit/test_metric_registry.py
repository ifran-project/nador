"""The metric registry: it describes NADOR's metrics and queries, and finds the entry of any series name."""

import re

import pytest

from nador.artifacts.prometheus_export import load_queries
from nador.config import METRIC_GROUPS
from nador.metric_registry import Family, Metric, load_registry


@pytest.fixture(scope="module")
def registry():
    return load_registry()


def test_every_named_query_is_described(registry):
    assert set(registry.queries) == {query["name"] for query in load_queries(None)}


def test_every_metric_a_query_reads_is_described_unless_the_query_says_why_it_returns_nothing(registry):
    for name, query in registry.queries.items():
        unknown = [metric for metric in query.reads if registry.describe(metric) is None]
        named = " ".join(query.problems)
        assert all(metric in named for metric in unknown), (name, unknown)


def test_the_series_of_a_histogram_are_described_by_the_histogram(registry):
    histogram = registry.describe("gtp_teid_latency_observed_ns")
    assert registry.describe("gtp_teid_latency_observed_ns_bucket") is histogram
    assert registry.describe("gtp_teid_latency_observed_ns_count") is histogram
    assert isinstance(histogram, Metric)
    assert histogram.type == "histogram"


def test_a_gauge_has_no_sum_series(registry):
    assert registry.describe("gtp_same_packet_upf_rtt_ns").type == "gauge"
    assert registry.describe("gtp_same_packet_upf_rtt_ns_sum") is None


def test_an_infrastructure_metric_is_described_by_its_longest_family_prefix(registry):
    family = registry.describe("node_load1")
    assert isinstance(family, Family)
    assert family.prefix == "node_"
    assert registry.describe("no_such_metric") is None


def test_each_per_ue_metric_names_the_labels_that_identify_its_ue(registry):
    assert registry.describe("ue_dl_brate").ue_labels[:2] == ["rnti", "pci"]
    assert set(registry.describe("gtp_throughput_bytes_total").ue_labels) >= {"imsi", "ue_ip", "slice"}


def test_the_profile_metrics_named_as_producers_exist(registry):
    named = [entry.produced_by for entry in [*registry.metrics.values(), *registry.families] if entry.produced_by]
    for text in named:
        for group, metric in re.findall(r"\b(ran|core|e2e|host)\.([a-z0-9_]+)", text):
            assert metric in METRIC_GROUPS[group], text


def test_the_entries_left_to_check_are_named_by_section(registry):
    checked = registry.model_copy(deep=True)
    checked.metrics["ue_cqi"].verified = True
    left = checked.unverified()
    assert "metrics.ue_cqi" not in left
    assert "metrics.ue_dl_brate" in left
    assert "queries.n3_throughput_bps" in left
