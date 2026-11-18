"""Profile validation: every rule keeps its user-facing message."""

import copy

import pytest
from conftest import EXAMPLES, example_profiles

from nador.config import MonitoringDeployment, read_deployment

CONTROLLER = "ghcr.io/ifran-project/nador-controller:sha-1"


@pytest.mark.parametrize("path", example_profiles().values(), ids=str)
def test_examples_are_valid(path):
    MonitoringDeployment.from_yaml_file(path)


def test_defaults_fill_controller_and_probe_images(quickstart):
    quickstart.pop("components")
    config = MonitoringDeployment.model_validate(quickstart)
    assert config.probe_controller_image.startswith("ghcr.io/ifran-project/nador-controller:")
    assert config.external_throughput_image.startswith("ghcr.io/ifran-project/nador-throughput-probe:")
    assert config.external_path_image is None


def without_role(profile, role):
    profile["targets"] = [t for t in profile["targets"] if t.get("role") != role]
    return profile


CASES = {
    "pairing needs latency": (
        lambda p: p | {"packet_pairing": True, "latency": False},
        "Packet pairing requires latency and both gNB and UPF targets",
    ),
    "rtt needs amf": (lambda p: without_role(p, "amf") | {"throughput": False}, "External RTT requires AMF/SMF"),
    "bad imsi": (lambda p: p | {"rtt_imsis": ["12"]}, "rtt_imsis must contain quoted IMSIs"),
    "bad slice": (lambda p: p | {"rtt_slices": ["1:2"]}, "rtt_slices must use labels such as 01:ffffff"),
    "duplicate target": (
        lambda p: p | {"targets": [*p["targets"], p["targets"][0]]},
        "Only one monitoring target per component is supported",
    ),
    "throughput needs upf": (
        lambda p: without_role(p, "upf") | {"latency": False},
        "Throughput requires AMF, SMF and UPF packet targets",
    ),
    "untagged controller": (
        lambda p: p | {"probe_controller_image": "ghcr.io/x/controller"},
        "probe_controller_image\n  Value error, must be a tagged image",
    ),
    "untagged throughput": (
        lambda p: p | {"external_throughput_image": "ghcr.io/x/tp"},
        "external_throughput_image\n  Value error, must be a tagged image",
    ),
    "api secrets in pairs": (
        lambda p: p | {"probe_api_tls_secret": "tls"},
        "Public probe API requires probe_controller_image and both API secrets",
    ),
    "node ports distinct": (
        lambda p: p | {"grafana_node_port": 30095},
        "Monitoring NodePorts must be distinct",
    ),
    "ngap needs amf": (
        lambda p: without_role(p, "amf") | {"latency": False, "throughput": False, "observability": {"ngap": True}},
        "NGAP/mapper coverage requires an AMF target",
    ),
    "metric port conflict": (
        lambda p: (
            p
            | {"observability": {"ngap": True}}
            | {"targets": [p["targets"][0] | {"metrics_port": 9102}, *p["targets"][1:]]}
        ),
        "amf: observability and NF metric ports conflict",
    ),
    "one-way needs image": (
        lambda p: p | {"one_way": {"n3": {"enabled": True}}},
        "Enabled one-way probes require one_way.image",
    ),
    "multi-anchor paths need explicit controller": (
        lambda p: (
            p
            | {"observability": {"user_plane_paths": True}}
            | {"targets": [*p["targets"], p["targets"][2] | {"component": "upf2"}]}
        ),
        "Set probe_controller_image to a build that supports the requested multi-anchor probes",
    ),
    "extended needs image": (
        lambda p: p | {"extended": {"scheduler_wait": True}},
        "extended.probe_images requires an image for: cpu",
    ),
    "path port range": (
        lambda p: p | {"observability": {"path_port": 9150}},
        "path_port overlaps the latency/throughput exporter ranges",
    ),
}


@pytest.mark.parametrize("name", CASES)
def test_invalid_profiles_explain_why(name, quickstart):
    change, message = CASES[name]
    profile = change(copy.deepcopy(quickstart))
    profile.pop("components", None)
    with pytest.raises(ValueError, match=message.replace("(", r"\(").replace(")", r"\)")):
        MonitoringDeployment.model_validate(profile)


def test_explicit_controller_is_kept(quickstart):
    quickstart.pop("components")
    config = MonitoringDeployment.model_validate(quickstart | {"probe_controller_image": CONTROLLER})
    assert config.probe_controller_image == CONTROLLER


def test_profiles_must_spell_out_switches(tmp_path):
    text = (EXAMPLES / "quickstart" / "monitoring.yaml").read_text().replace("loki: false\n", "")
    path = tmp_path / "monitoring.yaml"
    path.write_text(text)
    with pytest.raises(ValueError, match="loki: write an explicit true or false"):
        read_deployment(path)


def test_duplicate_keys_are_rejected(tmp_path):
    path = tmp_path / "monitoring.yaml"
    path.write_text((EXAMPLES / "quickstart" / "monitoring.yaml").read_text() + "loki: true\n")
    with pytest.raises(ValueError, match="duplicate monitoring option"):
        read_deployment(path)


def test_yes_is_not_a_boolean(tmp_path):
    path = tmp_path / "monitoring.yaml"
    path.write_text((EXAMPLES / "quickstart" / "monitoring.yaml").read_text().replace("loki: false", "loki: yes"))
    with pytest.raises(ValueError, match="loki"):
        read_deployment(path)


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
    from nador.config import is_pinned

    assert is_pinned(reference) is pinned


def test_one_way_and_extended_images_must_be_pinned():
    from nador.config import ExtendedSettings, OneWaySettings

    with pytest.raises(ValueError, match="must be a tagged image"):
        OneWaySettings(image="ghcr.io/x/one-way")
    with pytest.raises(ValueError, match="must be a tagged image"):
        ExtendedSettings(probe_images={"cpu": "ghcr.io/x/cpu"})


@pytest.mark.parametrize(
    ("value", "ok"), [("1s", True), ("15s", True), ("1h30m", True), ("5d", True), ("5 d", False), ("x", False)]
)
def test_prometheus_durations(quickstart, value, ok):
    quickstart.pop("components")
    profile = quickstart | {"prometheus_scrape_interval": value}
    if ok:
        MonitoringDeployment.model_validate(profile)
    else:
        with pytest.raises(ValueError, match="prometheus_scrape_interval"):
            MonitoringDeployment.model_validate(profile)


def test_rtt_placement_is_no_longer_accepted(quickstart):
    quickstart.pop("components")
    with pytest.raises(ValueError, match="rtt_placement"):
        MonitoringDeployment.model_validate(quickstart | {"rtt_placement": "external"})


def test_default_images_are_pinned_to_their_recorded_builds():
    from nador.config import DEFAULT_IMAGES

    builds = DEFAULT_IMAGES["builds"]
    digests = {build["digest"] for build in builds.values()}
    references = [*DEFAULT_IMAGES["probes"].values(), *DEFAULT_IMAGES["external_probes"].values()]
    references += [f"{c['repository']}:{c['image_tag']}" for c in DEFAULT_IMAGES["components"].values()]
    for reference in references:
        tag, _, digest = reference.rpartition("@")
        assert digest in digests, reference
        commit = next(b["source_commit"] for b in builds.values() if b["digest"] == digest)
        assert tag.endswith(f":sha-{commit}"), reference
