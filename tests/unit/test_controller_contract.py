"""The settings NADOR hands the controller keep the shape the controller image reads.

The controller is built in the nador-images repository; it reads these chart values as its
settings. A field renamed or dropped here breaks a deployed controller, so the shape is
fixed by this test for both ways of describing anchors.
"""

import pytest
from support import TARGETS, manager, network, options, profile

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
    "rtt": {"enabled", "allowed", "image", "imsis", "slices"},
    "throughput": {"enabled", "allowed", "image"},
    "userPlanePaths": {"enabled", "allowed", "image", "port", "interval"},
}
OPTIONAL_REQUEST_FIELDS = {"targets", "pairer_image"}
METRICS = {name: options(name) for name in ("rtt", "throughput", "user_plane_paths", "n3_one_way")}
CONTROLLER = "ghcr.io/ifran-project/nador-controller:sha-1"


def single_anchor():
    """One gNB and one UPF: the controller finds them under ``anchors``."""
    return manager(profile(metrics=METRICS))


def multi_anchor():
    """Two UPFs: each request carries its own list of targets."""
    targets = ["amf", "smf", "upf", TARGETS["upf"] | {"component": "upf2"}, "gnb"]
    config = profile(metrics=METRICS, targets=targets, images={"controller": CONTROLLER})
    return manager(config, network(upfs=2))


@pytest.fixture(params=[single_anchor, multi_anchor], ids=["single-anchor", "multi-anchor"])
def settings(request):
    return request.param().plan().components["monitoring-controller"].chart_values


def targets_in(settings):
    yield from settings["anchors"].values()
    yield from settings["oneWay"].get("one_way_targets", [])
    for request in REQUEST_FIELDS:
        yield from settings[request].get("targets", [])


def test_requests_have_the_fields_the_controller_reads(settings):
    assert ONE_WAY_FIELDS <= set(settings["oneWay"]) <= ONE_WAY_FIELDS | {"one_way_targets"}
    for request, fields in REQUEST_FIELDS.items():
        assert fields <= set(settings[request]) <= fields | OPTIONAL_REQUEST_FIELDS, request
    assert all(isinstance(running, bool) for running in settings["hostProbeStates"].values())
    assert set(settings["anchors"]) <= {"gnb", "upf"}


def test_every_target_has_the_same_fields(settings):
    found = list(targets_in(settings))
    assert found
    for target in found:
        assert set(target) == TARGET_FIELDS


def test_anchors_are_described_once_or_per_request():
    single = single_anchor().plan().components["monitoring-controller"].chart_values
    multi = multi_anchor().plan().components["monitoring-controller"].chart_values
    assert set(single["anchors"]) == {"gnb", "upf"}
    assert "targets" not in single["throughput"]
    assert multi["anchors"] == {}
    assert {t["component"] for t in multi["throughput"]["targets"]} == {"upf", "upf2"}
