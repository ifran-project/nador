"""Reading a monitoring profile: every mistake is refused with a message that says what to fix."""

import re

import pytest
import yaml
from support import TARGETS, options, profile

from nador.config import DEFAULT_IMAGES, MonitoringDeployment, validate_deployment

CONTROLLER = "ghcr.io/ifran-project/nador-controller:sha-1"
RTT = {"rtt": options("rtt")}


def check(data):
    return validate_deployment(data, explicit=True)


def write(tmp_path, data, text=None):
    path = tmp_path / "monitoring.yaml"
    path.write_text(text if text is not None else yaml.safe_dump(data, sort_keys=False))
    return path


# What a valid profile means


def test_a_profile_says_what_runs_now_and_what_can_start_later():
    config = check(
        profile(
            metrics={
                "rtt": options("rtt", packet_pairing=True),
                "core_logs": True,
                "scheduler_wait": options("scheduler_wait", on_demand=True),
            }
        )
    )
    metrics = config.metrics
    assert metrics.rtt.packet_pairing
    assert metrics.running("core_logs")
    assert metrics.log_groups == ["core"]
    assert metrics.available("scheduler_wait")
    assert not metrics.running("scheduler_wait")
    assert metrics.ngap is None
    assert config.uses_controller


def test_images_default_to_the_recorded_builds_and_can_be_overridden():
    config = check(profile(metrics=RTT, images={"controller": CONTROLLER}))
    assert config.image("controller") == CONTROLLER
    assert config.image("rtt_probe") == DEFAULT_IMAGES["rtt_probe"]


def test_metrics_are_written_back_in_their_groups():
    assert list(check(profile()).model_dump()["metrics"]) == ["ran", "core", "e2e", "host"]


def test_a_profile_without_its_defaults_is_written_and_read_back():
    # IFRAN's output() writes the deployment model with exclude_defaults=True
    config = check(profile(metrics={"rtt": options("rtt", packet_pairing=True), "core_logs": True}))
    written = config.model_dump(mode="json", exclude_defaults=True)
    assert "split_signaling" not in written["metrics"]["ran"]
    assert validate_deployment(written, explicit=False).metrics == config.metrics


# Mistakes in metrics, targets and options

CASES = {
    "packet pairing needs rtt running from the start": (
        profile(metrics={"rtt": options("rtt", packet_pairing=True, on_demand=True)}),
        "Packet pairing requires rtt to run from the start",
    ),
    "rtt needs the AMF": (profile(metrics=RTT, targets=["smf", "upf", "gnb"]), "rtt requires AMF and SMF targets"),
    "IMSIs are quoted digits": (
        profile(metrics={"rtt": options("rtt", imsis=["12"])}),
        "rtt imsis must contain quoted IMSIs",
    ),
    "slices are SST:SD labels": (
        profile(metrics={"rtt": options("rtt", slices=["1:2"])}),
        "rtt slices must use labels such as 01:ffffff",
    ),
    "packet pairing needs both anchors": (
        profile(metrics={"rtt": options("rtt", packet_pairing=True, at=["upf"])}),
        re.escape("rtt packet_pairing needs at: [gnb, upf]"),
    ),
    "rtt measures at a role that has a target": (
        profile(metrics={"rtt": options("rtt", at=["gnb"])}, targets=["amf", "smf", "upf"]),
        "rtt measures at gnb, but no target has that role",
    ),
    "each rtt anchor once": (profile(metrics={"rtt": options("rtt", at=["upf", "upf"])}), "at most once each"),
    "one target per component": (
        profile(targets=["amf", "smf", "upf", "gnb", "amf"]),
        "Only one monitoring target per component is supported",
    ),
    "throughput needs the UPF": (
        profile(metrics={"throughput": options("throughput")}, targets=["amf", "smf", "gnb"]),
        "throughput requires AMF, SMF and UPF packet targets",
    ),
    "throughput needs targets, like rtt": (
        profile(metrics={"throughput": options("throughput")}) | {"targets": []},
        "throughput requires AMF, SMF and UPF packet targets",
    ),
    "ngap needs the AMF": (profile(metrics={"ngap": options("ngap")}, targets=["smf"]), "ngap/ue_mapper_coverage"),
    "NADOR ports stay off the NF metrics port": (
        profile(
            metrics={"ngap": options("ngap")},
            targets=[TARGETS["amf"] | {"metrics_port": options("ngap")["port"]}, "smf", "upf", "gnb"],
        ),
        "amf: a NADOR metric port conflicts with the NF metrics port",
    ),
    "upf_transit needs a peer": (profile(metrics={"upf_transit": True}), "upf_transit.peer"),
    "tcp_flows needs its identity secret": (
        profile(metrics={"tcp_flows": True}),
        "tcp_flows.identity_secret",
    ),
    "path probe port stays out of the exporter ranges": (
        profile(metrics={"user_plane_paths": options("user_plane_paths", port=9150)}),
        "user_plane_paths port overlaps the latency/throughput exporter ranges",
    ),
    "several anchors need an explicit controller build": (
        profile(
            metrics={"user_plane_paths": options("user_plane_paths")},
            targets=["amf", "smf", "upf", TARGETS["upf"] | {"component": "upf2"}, "gnb"],
        ),
        "Set images.controller to a build that supports the requested multi-anchor probes",
    ),
    "only true and false switch a metric": (profile(metrics={"host_logs": "yes"}), "metrics.host.logs"),
}


@pytest.mark.parametrize("case", CASES)
def test_mistakes_are_explained(case):
    data, message = CASES[case]
    with pytest.raises(ValueError, match=message):
        check(data)


# Mistakes in the rest of the profile

SETTINGS = {
    "images need a tag": ({"images": {"controller": "ghcr.io/x/controller"}}, "must be a tagged image"),
    "image names are checked": ({"images": {"controler": CONTROLLER}}, "Unknown images"),
    "the mapper image needs a tag, not only a digest": (
        {"images": {"ue_mapper": "ghcr.io/x/mapper@sha256:abc"}},
        "images.ue_mapper must use an explicit repository:tag",
    ),
    "probe API secrets come in pairs": (
        {"probe_api_tls_secret": "tls"},
        "Public probe API requires a controller-managed metric and both API secrets",
    ),
    "NodePorts are distinct": ({"grafana_node_port": 30095}, "Monitoring NodePorts must be distinct"),
    "probe timeout fits in the interval": (
        {"tuning": {"probe_interval_seconds": 1, "probe_timeout_seconds": 2}},
        "probe_timeout_seconds must not exceed probe_interval_seconds",
    ),
    "scrape interval is a Prometheus duration": ({"prometheus_scrape_interval": "5 d"}, "prometheus_scrape_interval"),
}


@pytest.mark.parametrize("case", SETTINGS)
def test_setting_mistakes_are_explained(case):
    fields, message = SETTINGS[case]
    with pytest.raises(ValueError, match=message):
        check(profile(metrics=RTT, **fields))


@pytest.mark.parametrize("interval", ["1s", "15s", "1h30m", "5d"])
def test_prometheus_durations_are_accepted(interval):
    assert check(profile(prometheus_scrape_interval=interval)).prometheus_scrape_interval == interval


@pytest.mark.parametrize("section", ["latency", "observability", "extended", "one_way"])
def test_retired_sections_are_refused(section):
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        check(profile() | {section: {}})


# Profile files are explicit


def test_every_metric_must_be_listed(tmp_path):
    data = profile()
    del data["metrics"]["host"]["logs"]
    with pytest.raises(ValueError, match="metrics.host.logs: write true, false or its options"):
        MonitoringDeployment.from_yaml_file(write(tmp_path, data))


def test_a_metric_with_options_must_list_all_of_them(tmp_path):
    data = profile(metrics={"rtt": {"slices": ["01:ffffff"]}})
    expected = "rtt: {on_demand: false, at: [gnb, upf], packet_pairing: false, imsis: [], slices: [01:ffffff]}"
    with pytest.raises(ValueError, match=re.escape(expected)):
        MonitoringDeployment.from_yaml_file(write(tmp_path, data))
    with pytest.raises(ValueError, match="metrics.e2e.rtt: list every option"):
        MonitoringDeployment.from_yaml_file(write(tmp_path, profile(metrics={"rtt": True})))


def test_a_metric_must_sit_in_its_group():
    moved = profile(metrics=RTT)
    moved["metrics"]["core"]["rtt"] = moved["metrics"]["e2e"].pop("rtt")
    with pytest.raises(ValueError, match="metrics.core.rtt; it belongs under metrics.e2e.rtt"):
        check(moved)
    flat = profile(metrics=RTT)
    flat["metrics"]["rtt"] = flat["metrics"]["e2e"].pop("rtt")
    with pytest.raises(ValueError, match="rtt belongs under metrics.e2e.rtt"):
        check(flat)
    e2e_logs = profile()
    e2e_logs["metrics"]["e2e"]["logs"] = True
    with pytest.raises(ValueError, match="it belongs under metrics.ran.logs or metrics.core.logs"):
        check(e2e_logs)


def test_a_repeated_option_is_refused(tmp_path):
    text = yaml.safe_dump(profile(), sort_keys=False) + "namespace: other\n"
    with pytest.raises(ValueError, match="duplicate monitoring option"):
        MonitoringDeployment.from_yaml_file(write(tmp_path, None, text))


def test_yes_and_no_are_not_booleans(tmp_path):
    text = yaml.safe_dump(profile(), sort_keys=False).replace("logs: false", "logs: no")
    with pytest.raises(ValueError, match="metrics.ran.logs: Input should be"):
        MonitoringDeployment.from_yaml_file(write(tmp_path, None, text))
