"""CSI: the ``csi`` profile metric, how ``configure_csi`` applies it, the configuration it renders, its collection."""

import json
from contextlib import asynccontextmanager
from pathlib import Path

import jinja2
import pytest
from ansible.plugins.filter.core import FilterModule
from ansible.plugins.test.core import TestModule as AnsibleTests
from support import manager, matrix, network, options, profile

import ansible
from nador import configure_csi
from nador.artifacts import ExperimentRun, collectors
from nador.config import validate_deployment
from nador.csi import collection

CSI = {"csi": options("csi")}
NADOR_ASSETS = Path(__file__).resolve().parents[2] / "src" / "nador" / "assets"
IFRAN_TEMPLATES = Path(ansible.__file__).parent / "roles" / "helm" / "prepare" / "templates"
OAI_CONFIG = "gNBs:\n  - gNB_ID: 0x1\n    min_rxtxtime: 6\n    plmn_list:\n      - mcc: 001\n"
OCUDU_CONFIG = "gnb_id: 1\ncell_cfg:\n  dl_arfcn: 368500\n  pci: 1\n"


def ran(net=None, **metrics):
    """The RAN manager as a notebook holds it before ``ran.deploy()``, and the validated profile."""
    config = validate_deployment(profile(metrics=metrics or CSI), explicit=True)
    return manager(profile(), net or network()), config


NO_HOSTS = {"collect_setup": False, "collect_ue_context": False}


def through(url):
    @asynccontextmanager
    async def connection(monitoring):
        yield url

    return connection


def env(component):
    return component.extra_env_vars or {}


# The profile


def test_csi_options_reach_the_logger_environment():
    csi = options("csi", granularity="subcarrier", subcarrier_sampling=4, antennas="0,1", ports="0")
    config = validate_deployment(profile(metrics={"csi": csi}), explicit=True)
    assert config.metrics.csi.logger_env() == {
        "CSI_GRANULARITY": "subcarrier",
        "CSI_ANTENNA_SELECTION": "0,1",
        "CSI_PORT_SELECTION": "0",
        "CSI_SUBCARRIER_SAMPLING": "4",
    }


@pytest.mark.parametrize(
    ("given", "message"),
    [
        ({"srs_period_ms": 3}, "csi srs_period_ms must be one of 1, 2, 2.5"),
        ({"subcarrier_sampling": 2}, "csi subcarrier_sampling applies to granularity: subcarrier"),
        ({"antennas": "first"}, "metrics.ran.csi.antennas"),
        ({"output_dir": "data/csi"}, "metrics.ran.csi.output_dir"),
    ],
)
def test_csi_mistakes_are_explained(given, message):
    with pytest.raises(ValueError, match=message):
        validate_deployment(profile(metrics={"csi": options("csi", **given)}), explicit=True)


# Applying it to the RAN


def test_without_csi_the_ran_is_left_unchanged():
    managed, config = ran(ran_logs=True)
    before = managed.deployment_model.ran.model_dump()
    configure_csi(managed, config)
    assert managed.deployment_model.ran.model_dump() == before


def test_an_oai_gnb_logs_with_the_nador_charts():
    managed, config = ran()
    configure_csi(managed, config)
    gnb = managed.deployment_model.ran.components["gnb"]
    assert env(gnb) | {"CSI_LOGGING_ENABLED": "1", "CSI_OUTPUT_DIR": "/data/csi", "CSI_GRANULARITY": "rb"} == env(gnb)
    assert gnb.custom_chart_path.endswith("assets/ran/oai/oai-gnb")
    assert [Path(f).name for f in gnb.custom_rendered_files] == ["values.yaml.j2", "config.yaml.j2"]


def test_only_components_with_a_phy_log():
    managed, config = ran(network(layout="cu-du"))
    configure_csi(managed, config)
    components = managed.deployment_model.ran.components
    assert env(components["du"])["CSI_LOGGING_ENABLED"] == "1"
    assert "CSI_LOGGING_ENABLED" not in env(components["cu"])
    assert components["cu"].custom_chart_path is None


def test_a_component_without_a_phy_cannot_be_asked_for():
    managed, config = ran(network(layout="cu-du"), csi=options("csi", components=["cu"]))
    with pytest.raises(ValueError, match="cu: CSI logging is only supported on"):
        configure_csi(managed, config)


def test_an_ocudu_gnb_gets_its_srs_period_and_writes_under_mnt():
    managed, config = ran(network(ran="ocudu"), csi=options("csi", srs_period_ms=20))
    configure_csi(managed, config)
    gnb = managed.deployment_model.ran.components["gnb"]
    assert env(gnb) | {"CSI_ENABLED": "1", "CSI_SRS_PERIOD_MS": "20", "CSI_OUTPUT_DIR": "/mnt/csi"} == env(gnb)
    assert [Path(f).parent.name for f in gnb.custom_rendered_files] == ["ocudu", "ocudu"]


def test_ocudu_can_only_write_under_mnt():
    managed, config = ran(network(ran="ocudu"), csi=options("csi", output_dir="/data/csi"))
    with pytest.raises(ValueError, match="output_dir must be under it"):
        configure_csi(managed, config)


def test_a_deployed_ran_must_be_configured_before_it_was_deployed():
    managed, config = ran()
    managed.deployment_model.ran.components["gnb"].is_installed = True
    with pytest.raises(ValueError, match="gnb: configure CSI before deploying the RAN"):
        configure_csi(managed, config)


# The configuration the gNB is deployed with


def render(template, config_text, **csi_env):
    environment = jinja2.Environment(  # noqa: S701 - renders YAML, as IFRAN's Ansible role does
        loader=jinja2.FileSystemLoader([IFRAN_TEMPLATES, NADOR_ASSETS]), undefined=jinja2.StrictUndefined
    )
    environment.filters.update(FilterModule().filters())
    environment.tests.update(AnsibleTests().tests())
    component = {"type": "gnb", "plain_text_config": config_text, "extra_env_vars": csi_env or None}
    return environment.get_template(template).render(component=component).strip()


def test_without_csi_the_oai_configuration_is_the_upstream_one():
    assert render("ran/oai/config.yaml.j2", OAI_CONFIG) == OAI_CONFIG.strip()


def test_csi_adds_periodic_srs_to_the_oai_configuration_and_nothing_else():
    rendered = render("ran/oai/config.yaml.j2", OAI_CONFIG, CSI_LOGGING_ENABLED="1")
    expected = OAI_CONFIG.replace("min_rxtxtime: 6\n", "min_rxtxtime: 6\n    do_SRS: periodic\n    do_CSIRS: 1\n")
    assert rendered == expected.strip()


def test_an_oai_configuration_that_already_sets_srs_otherwise_fails_to_render():
    config = OAI_CONFIG + "    do_SRS: 0\n"
    with pytest.raises(jinja2.UndefinedError, match="csi_config_injection_failed"):
        render("ran/oai/config.yaml.j2", config, CSI_LOGGING_ENABLED="1")


def test_csi_adds_periodic_srs_to_the_ocudu_cell():
    assert render("ran/ocudu/config.yaml.j2", OCUDU_CONFIG) == OCUDU_CONFIG.strip()
    rendered = render("ran/ocudu/config.yaml.j2", OCUDU_CONFIG, CSI_LOGGING_ENABLED="1", CSI_SRS_PERIOD_MS="20")
    assert rendered.startswith("gnb_id: 1\ncell_cfg:\n  srs:\n    type_enabled: periodic\n    period_ms: 20\n")


# Collecting the CSI file with a run


@pytest.fixture
def csi_node(monkeypatch, http):
    """A gNB node whose clock is ``offset`` seconds ahead, and the Ansible role that cuts the CSI file on it."""
    node = {"offsets": [2.0, 2.0], "fetches": []}

    async def measure_clock(self, samples=5):
        return {"offset_s": node["offsets"].pop(0), "rtt_s": 0.01, "uncertainty_s": 0.005, "node": self.node_name}

    async def run_ansible_role(*, role, inputs, **_):
        node["fetches"].append((role, json.loads(Path(inputs["csi_windows_json"]).read_text())))
        return {"csi_summary": {"rows": 10, "dropped_rows": node.get("dropped", 0)}}

    monkeypatch.setattr(collection.CsiCollector, "measure_clock", measure_clock)
    monkeypatch.setattr(collection, "run_ansible_role", run_ansible_role)
    http.routes["/api/v1/query_range"] = lambda q: matrix(({}, [(float(q["start"]), "1")]))
    monkeypatch.setattr(collectors, "prometheus_connection", through(http.url))
    return node


async def csi_run(directory, **metrics):
    config = validate_deployment(profile(metrics=metrics or CSI), explicit=True)
    monitoring = manager(config)
    configure_csi(monitoring, config)
    async with ExperimentRun(directory, timezone="UTC", monitoring=monitoring, **NO_HOSTS) as run:
        with run.section("work"):
            pass
    return json.loads((directory / "experiment_metadata.json").read_text())


async def test_the_csi_file_is_cut_by_the_run_windows_on_the_gnb_clock(tmp_path, csi_node):
    metadata = await csi_run(tmp_path / "run")
    ((role, windows),) = csi_node["fetches"]
    assert role == "csi/collect"
    section = next(w for w in windows["windows"] if w["level"] == "section")
    assert section["ran_start_epoch"] == section["start_epoch"] + 2.0
    assert metadata["csi"]["gnb"]["offset_applied_s"] == 2.0
    assert metadata["csi"]["gnb"]["summary"]["rows"] == 10
    assert metadata["csi"]["gnb"]["warnings"] == []
    assert (tmp_path / "run" / "csi" / "gnb" / "csi_collection.json").exists()


async def test_a_small_clock_offset_is_not_applied(tmp_path, csi_node):
    csi_node["offsets"] = [0.2, 0.2]
    metadata = await csi_run(tmp_path / "run")
    assert metadata["csi"]["gnb"]["offset_applied_s"] == 0.0


async def test_a_clock_that_moved_and_dropped_rows_are_warned_about(tmp_path, csi_node):
    csi_node["offsets"] = [2.0, 3.0]
    csi_node["dropped"] = 5
    metadata = await csi_run(tmp_path / "run")
    first, second = metadata["csi"]["gnb"]["warnings"]
    assert "clock offset changed by +1.000 s" in first
    assert second == "the logger dropped 5 rows (buffer full)"


async def test_without_csi_nothing_is_collected(tmp_path, csi_node):
    await csi_run(tmp_path / "run", scheduler_wait=options("scheduler_wait"))
    assert csi_node["fetches"] == []
