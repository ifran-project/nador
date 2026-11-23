"""CSI: the ``csi`` profile metric, how ``configure_csi`` applies it, the configuration it renders, its collection."""

import asyncio
import json
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path

import jinja2
import pytest
from ansible.plugins.filter.core import FilterModule
from ansible.plugins.test.core import TestModule as AnsibleTests
from ifran.models.k8s import ContainerImage
from support import manager, matrix, network, options, profile

import ansible
from nador import configure_ran_monitoring
from nador import csi as csi_module
from nador.artifacts import ExperimentRun, collectors
from nador.config import validate_deployment
from nador.csi import CsiLiveView, collection, configure_csi, live, prepare_csi_storage, storage

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


def test_one_call_prepares_the_ran_for_the_whole_profile():
    metrics = {"csi": options("csi"), "ran_kpis": options("ran_kpis")}
    managed, config = ran(network(ran="ocudu"), **metrics)
    configure_ran_monitoring(managed, config)
    gnb = managed.deployment_model.ran.components["gnb"]
    assert env(gnb)["CSI_ENABLED"] == "1"
    assert gnb.advanced_config.model_dump(exclude_none=True)["metrics"]["enable_json"] is True


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


async def csi_run(directory, *, collect=True, **metrics):
    config = validate_deployment(profile(metrics=metrics or CSI), explicit=True)
    monitoring = manager(config)
    configure_csi(monitoring, config)
    async with ExperimentRun(directory, timezone="UTC", monitoring=monitoring, collect_csi=collect, **NO_HOSTS) as run:
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


async def test_csi_is_collected_only_when_the_run_asks(tmp_path, csi_node):
    metadata = await csi_run(tmp_path / "run", collect=False)
    assert csi_node["fetches"] == []
    assert "csi" not in metadata


async def test_without_csi_nothing_is_collected(tmp_path, csi_node):
    await csi_run(tmp_path / "run", scheduler_wait=options("scheduler_wait"))
    assert csi_node["fetches"] == []


# Images


def image(reference):
    repository, tag = reference.rsplit(":", 1)
    return ContainerImage(repository=repository, image_tag=tag)


def test_the_logger_image_is_built_from_the_same_oai_tag():
    managed, config = ran(network(layout="cu-du"))
    components = managed.deployment_model.ran.components
    components["du"].image = image("docker.io/oaisoftwarealliance/oai-gnb:2026.w42")
    components["cu"].image = image("docker.io/r2labuser/oai-gnb-csi:2026.w42")  # no PHY: back to the plain image
    configure_csi(managed, config)
    assert components["du"].image == image("docker.io/r2labuser/oai-gnb-csi:2026.w42")
    assert components["cu"].image == image("docker.io/r2labuser/oai-gnb:2026.w42")


def test_the_ocudu_logger_image_of_the_radio_variant():
    managed, config = ran(network(ran="ocudu", layout="cu-du"))
    du = managed.deployment_model.ran.components["du"]
    du.image = image("registry.gitlab.com/ocudu/ocudu/images/du-uhd:ubuntu-24.04-avx2-20261001_920f88c7-stable")
    configure_csi(managed, config)
    assert du.image == image("docker.io/r2labuser/ocudu-du-csi:release_26_10-uhd")


def test_an_image_without_a_logger_build_is_refused():
    managed, config = ran()
    managed.deployment_model.ran.components["gnb"].image = image("docker.io/someone/oai-gnb:dev")
    with pytest.raises(ValueError, match="no CSI image known for 'docker.io/someone/oai-gnb'"):
        configure_csi(managed, config)


# Storage, live view and clock on the gNB node


def csi_ran(net=None):
    managed, config = ran(net)
    configure_csi(managed, config)
    return managed


async def test_the_csi_directory_of_each_node_is_checked(monkeypatch):
    calls = []

    async def run_ansible_role(*, role, inputs, hosts, **_):
        calls.append((role, hosts, inputs))
        return {"csi_storage": {"before": "tmpfs", "after": "ext4"}}

    monkeypatch.setattr(storage, "run_ansible_role", run_ansible_role)
    report = await prepare_csi_storage(csi_ran(), {"node1": "/dev/sda1"})
    assert report == {
        "node1:/data/csi": {"before": "tmpfs", "after": "ext4", "node": "node1", "directory": "/data/csi"}
    }
    ((role, hosts, inputs),) = calls
    assert (role, hosts, inputs["csi_storage_device"]) == ("csi/prepare_node", "node1", "sda1")


async def test_storage_needs_a_csi_component_on_each_named_node():
    with pytest.raises(ValueError, match="No CSI component on \\['node9'\\]"):
        await prepare_csi_storage(csi_ran(), {"node9": "sda1"})
    with pytest.raises(ValueError, match="No CSI-enabled component"):
        await prepare_csi_storage(ran()[0])


def two_nodes(managed):
    nodes = managed.deployment_model.nodes
    nodes["node2"] = nodes["node1"].model_copy(deep=True)
    return managed


async def test_the_live_view_runs_off_the_gnb_node(monkeypatch):
    managed = two_nodes(csi_ran())
    with pytest.raises(ValueError, match="must not run on the gNB node"):
        CsiLiveView(managed, "node1")
    actions = []

    async def run_ansible_role(*, inputs, hosts, **_):
        actions.append((inputs["csi_live_action"], hosts))
        return {"csi_live_status": ["streamer running", "viewer running"]}

    async def open_tunnel(self, local_port=None, timeout_s=20):
        return "http://127.0.0.1:8501"

    monkeypatch.setattr(live, "run_ansible_role", run_ansible_role)
    monkeypatch.setattr(CsiLiveView, "open_tunnel", open_tunnel)
    view = CsiLiveView(managed, "node2", window_s=60)
    assert view.inputs["csi_output_dir"] == "/data/csi"
    assert view.inputs["csi_live_window_s"] == 60
    assert await view.start() == "http://127.0.0.1:8501"
    assert await view.status() == "streamer running\nviewer running"
    await view.stop()
    assert actions == [("start", "node2"), ("status", "node2"), ("stop", "node2")]
    assert "-L 127.0.0.1:9000:127.0.0.1:8501" in view.tunnel_command(9000)


async def test_the_gnb_clock_is_read_with_the_smallest_round_trip(monkeypatch):
    class Date:
        returncode = 0

        async def communicate(self):
            return f"{time.time() + 3:.6f}\n".encode(), b""

    commands = []

    async def create_subprocess_exec(*command, **_):
        commands.append(command)
        return Date()

    monkeypatch.setattr(collection.asyncio, "create_subprocess_exec", create_subprocess_exec)
    clock = await collection.CsiCollector(csi_ran(), "gnb").measure_clock(samples=3)
    assert clock["offset_s"] == pytest.approx(3, abs=0.1)
    assert (clock["samples"], clock["node"]) == (3, "node1")
    assert commands[0][-2:] == ("date", "+%s.%N")


def test_a_component_without_csi_cannot_be_collected():
    with pytest.raises(ValueError, match="gnb: CSI logging is not enabled"):
        collection.CsiCollector(ran()[0], "gnb")
    with pytest.raises(ValueError, match="Unknown RAN component 'du9'"):
        collection.CsiCollector(csi_ran(), "du9")


class Ssh:
    """The ssh process of the tunnel: running, or exited with its error in the log."""

    pid = 0

    def __init__(self, argv, stderr, exit_code=None):
        self.argv, self.returncode, self.terminated = argv, exit_code, False
        if exit_code is not None:
            stderr.write("Permission denied (publickey)")

    def terminate(self):
        self.terminated = True

    async def wait(self):
        return 0


@pytest.fixture
def tunnel(monkeypatch, tmp_path):
    view = CsiLiveView(two_nodes(csi_ran()), "node2")
    view.tunnel_log = tmp_path / "tunnel.log"
    started = []

    async def popen(*argv, stdout, stderr, exit_code=None):
        started.append(Ssh(argv, stderr, exit_code))
        return started[-1]

    monkeypatch.setattr(live.asyncio, "create_subprocess_exec", popen)
    monkeypatch.setattr(live.atexit, "register", lambda *_: None)
    return view, started, popen


async def test_the_tunnel_is_ready_when_its_local_port_answers(tunnel, monkeypatch):
    view, started, _ = tunnel
    server = await asyncio.start_server(lambda reader, writer: writer.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        view.port = port  # the forwarded port is already served locally: only its readiness is tested here
        monkeypatch.setattr(live, "_local_port_free", lambda _: True)
        assert await view.open_tunnel() == f"http://127.0.0.1:{port}"
        assert f"127.0.0.1:{port}:127.0.0.1:{port}" in started[0].argv
        await view.close_tunnel()
    assert started[0].terminated
    assert view.tunnel is None


async def test_a_tunnel_that_ssh_refuses_reports_why(tunnel, monkeypatch):
    view, _, popen = tunnel
    monkeypatch.setattr(live.asyncio, "create_subprocess_exec", lambda *argv, **kw: popen(*argv, **kw, exit_code=255))
    with pytest.raises(RuntimeError, match="CSI live tunnel failed: Permission denied"):
        await view.open_tunnel(timeout_s=2)


async def test_a_busy_local_port(tunnel):
    view, _, _ = tunnel
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        port = busy.getsockname()[1]
        with pytest.raises(RuntimeError, match=f"Local port {port} is already in use"):
            await view.open_tunnel(port)


# Several loggers, and images without CSI


def second_gnb(managed, node="node1"):
    components = managed.deployment_model.ran.components
    components["gnb2"] = components["gnb"].model_copy(deep=True, update={"node_name": node})
    return managed


def test_two_loggers_cannot_write_the_same_file():
    managed, config = ran()
    second_gnb(managed)
    with pytest.raises(ValueError, match="write /data/csi/csi_per_rb.csv on the same node"):
        configure_csi(managed, config)
    second_gnb(managed, node=None)
    with pytest.raises(ValueError, match="share /data/csi: set node_name on each"):
        configure_csi(managed, config)
    second_gnb(managed, node="node2")
    configure_csi(managed, config)


def test_an_unknown_component_is_named():
    managed, config = ran(csi=options("csi", components=["du9"]))
    with pytest.raises(ValueError, match="Unknown RAN components: \\['du9'\\]"):
        configure_csi(managed, config)


def test_components_without_csi_get_the_image_without_logger():
    managed = ran(network(layout="cu-du"))[0]
    components = managed.deployment_model.ran.components
    components["du"].image = image("docker.io/r2labuser/oai-gnb-csi:2026.w42")
    components["cu"].image = image("docker.io/r2labuser/oai-gnb-aw2s-csi:2026.w42")
    csi_module._select_csi_images(managed)
    assert components["du"].image == image("docker.io/r2labuser/oai-gnb:2026.w42")
    assert components["cu"].image == image("docker.io/r2labuser/oai-gnb-aw2s:2026.w42")


def test_an_ocudu_du_without_csi_goes_back_to_its_upstream_image():
    managed = ran(network(ran="ocudu", layout="cu-du"))[0]
    du = managed.deployment_model.ran.components["du"]
    du.image = image("docker.io/r2labuser/ocudu-du-csi:release_26_10-uhd")
    csi_module._select_csi_images(managed)
    assert du.image.repository == "registry.gitlab.com/ocudu/ocudu/images/du-uhd"
