"""Profile validation: every rule keeps a message that says what to fix."""

import copy
import json
from pathlib import Path

import pytest
from conftest import EXAMPLES, example_profiles

from nador.config import DEFAULT_IMAGES, Metrics, MonitoringDeployment, is_pinned, read_deployment

CONTROLLER = "ghcr.io/ifran-project/nador-controller:sha-1"
QUICKSTART = EXAMPLES / "quickstart" / "monitoring.yaml"


@pytest.mark.parametrize("path", example_profiles().values(), ids=str)
def test_examples_are_valid(path):
    MonitoringDeployment.from_yaml_file(path)


@pytest.fixture
def profile(quickstart):
    quickstart.pop("components")
    return quickstart


def with_metrics(profile, **metrics):
    return profile | {"metrics": profile["metrics"] | metrics}


def without_role(profile, role):
    return profile | {"targets": [t for t in profile["targets"] if t.get("role") != role]}


def test_metric_switches(profile):
    config = MonitoringDeployment.model_validate(
        with_metrics(profile, rtt={"packet_pairing": True}, logs=True, scheduler_wait={"on_demand": True})
    )
    metrics = config.metrics
    assert metrics.rtt.packet_pairing
    assert metrics.running("logs")
    assert metrics.available("scheduler_wait")
    assert not metrics.running("scheduler_wait")
    assert metrics.ngap is None
    assert config.uses_controller
    assert config.host_probe_images() == {"cpu": DEFAULT_IMAGES["cpu_probe"]}


def test_defaults_come_from_images_json(profile):
    config = MonitoringDeployment.model_validate(profile | {"images": {"controller": CONTROLLER}})
    assert config.image("controller") == CONTROLLER
    assert config.image("throughput_probe") == DEFAULT_IMAGES["throughput_probe"]


CASES = {
    "pairing needs running rtt": (
        lambda p: with_metrics(p, rtt={"packet_pairing": True, "on_demand": True}),
        "Packet pairing requires a running rtt metric",
    ),
    "rtt needs amf": (
        lambda p: with_metrics(without_role(p, "amf"), throughput=False),
        "rtt requires AMF/SMF and a gNB or UPF target",
    ),
    "bad imsi": (lambda p: with_metrics(p, rtt={"imsis": ["12"]}), "rtt imsis must contain quoted IMSIs"),
    "bad slice": (lambda p: with_metrics(p, rtt={"slices": ["1:2"]}), "rtt slices must use labels such as 01:ffffff"),
    "duplicate target": (
        lambda p: p | {"targets": [*p["targets"], p["targets"][0]]},
        "Only one monitoring target per component is supported",
    ),
    "throughput needs upf": (
        lambda p: with_metrics(without_role(p, "upf"), rtt=False),
        "throughput requires AMF, SMF and UPF packet targets",
    ),
    "untagged image": (lambda p: p | {"images": {"controller": "ghcr.io/x/controller"}}, "must be a tagged image"),
    "unknown image": (lambda p: p | {"images": {"controler": CONTROLLER}}, "Unknown images"),
    "mapper image needs a tag": (
        lambda p: p | {"images": {"ue_mapper": "ghcr.io/x/mapper@sha256:abc"}},
        "images.ue_mapper must use an explicit repository:tag",
    ),
    "api secrets in pairs": (
        lambda p: p | {"probe_api_tls_secret": "tls"},
        "Public probe API requires a controller-managed metric and both API secrets",
    ),
    "node ports distinct": (lambda p: p | {"grafana_node_port": 30095}, "Monitoring NodePorts must be distinct"),
    "ngap needs amf": (
        lambda p: with_metrics(without_role(p, "amf"), rtt=False, throughput=False, ngap=True),
        "ngap/ue_mapper_coverage require an AMF target",
    ),
    "metric port conflict": (
        lambda p: (
            with_metrics(p, ngap=True) | {"targets": [p["targets"][0] | {"metrics_port": 9102}, *p["targets"][1:]]}
        ),
        "amf: a NADOR metric port conflicts with the NF metrics port",
    ),
    "upf transit needs a peer": (lambda p: with_metrics(p, upf_transit=True), "upf_transit.peer"),
    "multi-anchor paths need explicit controller": (
        lambda p: (
            with_metrics(p, user_plane_paths=True)
            | {"targets": [*p["targets"], p["targets"][2] | {"component": "upf2"}]}
        ),
        "Set images.controller to a build that supports the requested multi-anchor probes",
    ),
    "tcp flows need a secret": (lambda p: with_metrics(p, tcp_flows=True), "tcp_flows.identity_secret"),
    "path port range": (
        lambda p: with_metrics(p, user_plane_paths={"port": 9150}),
        "user_plane_paths port overlaps the latency/throughput exporter ranges",
    ),
    "yes is not a switch": (lambda p: with_metrics(p, logs="yes"), "metrics.logs"),
    "probe timeout within interval": (
        lambda p: p | {"tuning": {"probe_interval_seconds": 1, "probe_timeout_seconds": 2}},
        "probe_timeout_seconds must not exceed probe_interval_seconds",
    ),
}


@pytest.mark.parametrize("name", CASES)
def test_invalid_profiles_explain_why(name, profile):
    change, message = CASES[name]
    with pytest.raises(ValueError, match=message.replace("(", r"\(").replace(")", r"\)")):
        MonitoringDeployment.model_validate(change(copy.deepcopy(profile)))


def test_profiles_must_list_every_metric(tmp_path):
    path = tmp_path / "monitoring.yaml"
    path.write_text(QUICKSTART.read_text().replace("  logs: false\n", ""))
    with pytest.raises(ValueError, match="metrics.logs: write true, false or its options"):
        read_deployment(path)


def test_duplicate_keys_are_rejected(tmp_path):
    path = tmp_path / "monitoring.yaml"
    path.write_text(QUICKSTART.read_text() + "namespace: other\n")
    with pytest.raises(ValueError, match="duplicate monitoring option"):
        read_deployment(path)


def test_only_true_and_false_are_booleans(tmp_path):
    path = tmp_path / "monitoring.yaml"
    path.write_text(QUICKSTART.read_text().replace("logs: false", "logs: no"))
    with pytest.raises(ValueError, match="metrics.logs"):
        read_deployment(path)


@pytest.mark.parametrize(("value", "ok"), [("1s", True), ("15s", True), ("1h30m", True), ("5d", True), ("5 d", False)])
def test_prometheus_durations(profile, value, ok):
    data = profile | {"prometheus_scrape_interval": value}
    if ok:
        MonitoringDeployment.model_validate(data)
    else:
        with pytest.raises(ValueError, match="prometheus_scrape_interval"):
            MonitoringDeployment.model_validate(data)


def test_retired_sections_are_rejected(profile):
    for retired in ({"latency": True}, {"observability": {}}, {"extended": {}}, {"one_way": {}}):
        with pytest.raises(ValueError, match="Extra inputs are not permitted"):
            MonitoringDeployment.model_validate(profile | retired)


@pytest.mark.parametrize(
    ("reference", "pinned"),
    [
        ("ghcr.io/x/y:v1", True),
        ("ghcr.io/x/y@sha256:abc", True),
        ("ghcr.io/x/y:v1@sha256:abc", True),
        ("registry:5000/x/y", False),
        ("ghcr.io/x/y", False),
        ("ghcr.io/x/y: v1", False),
        ("", False),
    ],
)
def test_image_pinning(reference, pinned):
    assert is_pinned(reference) is pinned


def test_every_metric_is_known_to_the_metrics_model():
    assert len(Metrics.model_fields) == 20


def test_default_images_are_pinned_to_their_recorded_builds():
    data = json.loads((Path(__file__).parents[1] / "src/nador/assets/images.json").read_text())
    digests = {build["digest"]: build["source_commit"] for build in data["builds"].values()}
    for key, reference in data["images"].items():
        name, _, digest = reference.rpartition("@")
        assert digest in digests, key
        tag = name.rsplit("/", 1)[-1].partition(":")[2]
        assert tag in ("", f"sha-{digests[digest]}"), key
