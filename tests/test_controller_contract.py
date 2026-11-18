"""The settings the controller image reads keep the shape documented in docs/controller-contract.md."""

import json

import pytest
from conftest import SNAPSHOTS

TARGET_FIELDS = {
    "category",
    "component",
    "role",
    "interface",
    "container",
    "selector",
    "metrics_port",
    "metrics_path",
    "latency_mode",
    "n3_interface",
    "n6_interface",
    "n3_network_names",
    "namespace",
    "implementation",
    "label_selectors",
}
ONE_WAY_FIELDS = {"image", "sample", "test_port", "n3_one_way", "upf_transit"}
REQUEST_FIELDS = {
    "rttRequest": {"enabled", "allowed", "image", "imsis", "slices"},
    "throughputRequest": {"enabled", "allowed", "image"},
    "pathRequest": {"enabled", "allowed", "image", "port", "interval"},
}
OPTIONAL_REQUEST_FIELDS = {"targets", "pairer_image"}


def controller_values():
    for path in sorted(SNAPSHOTS.glob("*.json")):
        components = json.loads(path.read_text())["components"]
        if "monitoring-controller" in components:
            yield path.stem, components["monitoring-controller"]["chart_values"]


CASES = dict(controller_values())


def all_targets(values):
    yield from values["probeControlTargets"].values()
    yield from values["probeControl"].get("one_way_targets", [])
    for request in REQUEST_FIELDS:
        yield from values[request].get("targets", [])


@pytest.mark.parametrize("name", sorted(CASES))
def test_settings_shape(name):
    values = CASES[name]
    assert ONE_WAY_FIELDS <= set(values["probeControl"]) <= ONE_WAY_FIELDS | {"one_way_targets"}
    for request, fields in REQUEST_FIELDS.items():
        assert fields <= set(values[request]) <= fields | OPTIONAL_REQUEST_FIELDS, request
    assert all(isinstance(state, bool) for state in values["hostProbeStates"].values())
    assert set(values["probeControlTargets"]) <= {"gnb", "upf"}


@pytest.mark.parametrize("name", sorted(CASES))
def test_target_shape(name):
    for target in all_targets(CASES[name]):
        assert set(target) == TARGET_FIELDS


def test_contract_cases_cover_single_and_multi_anchor():
    assert any(values["probeControlTargets"] for values in CASES.values())
    assert any("targets" in values["throughputRequest"] for values in CASES.values())
