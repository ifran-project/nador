"""What a profile deploys: which Helm releases, where, and with which settings.

Each test plans a small network with ``MonitoringManagement.plan()``, which contacts nothing,
and checks one rule.
"""

import pytest
from metamodel_3gpp.models.metamodel import MetaModel
from pydantic import BaseModel
from support import TARGETS, manager, network, options, profile

from nador.planner import REMOTE_CHARTS, ocudu_dus

ALWAYS = {"integration", "prometheus", "grafana"}
SESSION_STACK = {"redis", "ue-mapper-api", "monitoring-controller"}
CONTROLLER = "ghcr.io/ifran-project/nador-controller:sha-1"


def releases(config, net=None):
    return set(manager(config, net).plan().components)


def controller(config, net=None):
    return manager(config, net).plan().components["monitoring-controller"].chart_values


def second_upf():
    return TARGETS["upf"] | {"component": "upf2", "container": "upf"}


# Releases


def test_with_every_metric_off_only_the_base_stack_is_deployed():
    assert releases(profile()) == ALWAYS


@pytest.mark.parametrize("metric", ["rtt", "throughput"])
def test_session_metrics_bring_the_session_stack(metric):
    assert releases(profile(metrics={metric: options(metric)})) == ALWAYS | SESSION_STACK


@pytest.mark.parametrize("logs", ["ran_logs", "core_logs", "host_logs"])
def test_any_logs_bring_loki_and_promtail(logs):
    assert releases(profile(metrics={logs: True})) == ALWAYS | {"loki", "promtail"}


@pytest.mark.parametrize(("ran", "calculator"), [("oai", "kpi-calculator-oai"), ("ocudu", "ocudu-metrics")])
def test_ran_kpis_use_the_calculator_of_the_ran(ran, calculator):
    expected = ALWAYS | {calculator, "rnti-mapper"}
    assert releases(profile(metrics={"ran_kpis": options("ran_kpis")}), network(ran=ran)) == expected


def test_csi_brings_the_rnti_mapper():
    assert releases(profile(metrics={"csi": options("csi")})) == ALWAYS | {"rnti-mapper"}


def test_the_rnti_mapper_reads_the_rrc_and_names_the_ues():
    net = network()
    net.metamodel = MetaModel.model_validate(
        net.metamodel.model_dump(mode="json") | {"ues": {"ue1": {"config": {"supi": "001010000000001"}}}}
    )
    mapper = manager(profile(metrics={"csi": options("csi"), "rtt": options("rtt")}), net).plan().components
    values = mapper["rnti-mapper"].chart_values
    assert values["sources"] == [{"component": "gnb", "namespace": "ran", "selector": "app=oai-gnb", "type": "oai"}]
    assert values["core"] == {"type": "open5gs", "namespace": "core", "selector": "app=open5gs-amf", "container": "amf"}
    assert values["ueNames"] == ["001010000000001 ue1"]
    assert values["ocuduDus"] == []  # OAI: the mapper reads the DU of each UE from the RRC statistics
    assert values["inventoryUrl"] == "http://ue-mapper-api.monitoring.svc.cluster.local:8080/inventory/ues"
    assert values["image"]["repository"] == "ghcr.io/ifran-project/nador-rnti-mapper"


def test_the_rnti_mapper_names_the_dus_of_an_ocudu_cu_cp():
    net = network(ran="ocudu", layout="cucp-cuup-2du")
    csi = options("csi", components=["du01"])
    mapper = manager(profile(metrics={"csi": csi}, targets=["amf", "smf", "upf"]), net).plan().components
    assert mapper["rnti-mapper"].chart_values["ocuduDus"] == ["cucp 1 du01", "cucp 2 du02"]


class DUAdvancedConfig(BaseModel):
    """A DU advanced_config with gnb_du_id, as IFRAN's fhi72 branch has (released IFRAN: gNB only)."""

    gnb_du_id: int = 0


def test_the_gnb_du_id_of_the_advanced_config_wins_and_a_shared_id_names_no_du():
    net = network(ran="ocudu", layout="cucp-cuup-2du")
    du02 = net.deployment_model.ran.components["du02"]
    object.__setattr__(du02, "advanced_config", DUAdvancedConfig(gnb_du_id=7))
    assert ocudu_dus(net.metamodel, net.deployment_model) == ["cucp 1 du01", "cucp 7 du02"]
    object.__setattr__(du02, "advanced_config", DUAdvancedConfig())  # not set: gnbDuId of the meta model
    assert ocudu_dus(net.metamodel, net.deployment_model) == ["cucp 1 du01", "cucp 2 du02"]
    object.__setattr__(du02, "advanced_config", DUAdvancedConfig(gnb_du_id=1))
    assert ocudu_dus(net.metamodel, net.deployment_model) == []  # du01 and du02 would both be du=1
    assert ocudu_dus(None, net.deployment_model) == []
    oai = network(ran="oai", layout="cucp-cuup-2du")
    assert ocudu_dus(oai.metamodel, oai.deployment_model) == []


def test_ran_kpis_are_refused_for_a_ran_without_a_calculator():
    with pytest.raises(ValueError, match="RAN KPIs support OAI and OCUDU"):
        manager(profile(metrics={"ran_kpis": options("ran_kpis")}), network(ran="ueransim")).plan()


def test_each_host_probe_is_its_own_release():
    config = profile(metrics={"scheduler_wait": options("scheduler_wait"), "kernel_drops": options("kernel_drops")})
    assert {name for name in releases(config) if name.startswith("probe-")} == {"probe-cpu", "probe-drops"}


def test_component_overrides_are_merged_into_the_release():
    config = profile(
        metrics={"rtt": options("rtt")},
        components={"ue-mapper-api": {"image": {"image_pull_policy": "Always"}}},
    )
    mapper = manager(config).plan().components["ue-mapper-api"]
    assert mapper.image.image_pull_policy == "Always"
    assert mapper.image.repository  # the rest of the default image is kept


def test_local_charts_are_installed_from_the_control_node():
    for component in manager(profile(metrics={"rtt": options("rtt")})).plan().components.values():
        assert component.chart_version or component.chart_ref.startswith(f"{REMOTE_CHARTS}/")
        assert component.namespace == "monitoring"


# Targets


def test_targets_are_selected_by_the_labels_ifran_gives_them():
    targets = {t.component: t for t in manager(profile()).plan().targets}
    assert targets["amf"].selector == {"app": "open5gs-amf"}
    assert targets["amf"].namespace == "core"
    assert targets["gnb"].selector == {"app": "oai-gnb"}
    assert targets["gnb"].label_selectors == ["app=oai-gnb"]


def test_an_explicit_selector_wins():
    gnb = TARGETS["gnb"] | {"selector": {"app.kubernetes.io/name": "my-gnb"}}
    targets = {t.component: t for t in manager(profile(targets=["amf", "smf", "upf", gnb])).plan().targets}
    assert targets["gnb"].selector == {"app.kubernetes.io/name": "my-gnb"}


def test_a_target_must_exist_in_the_network():
    with pytest.raises(ValueError, match="Unknown core component 'upf2'"):
        manager(profile(targets=["amf", "smf", "upf", second_upf()])).plan()


def test_a_role_must_match_the_component_type():
    wrong = TARGETS["smf"] | {"role": "upf"}
    with pytest.raises(ValueError, match="smf: smf cannot be a upf packet target"):
        manager(profile(targets=["amf", wrong])).plan()


def test_a_component_that_is_not_deployed_yet_cannot_be_targeted():
    net = network()
    net.deployment_model.ran.components["gnb"].namespace = None
    with pytest.raises(ValueError, match="gnb: namespace is unresolved"):
        manager(profile(), net).plan()


# Session metrics on the controller


def test_rtt_measures_at_both_anchors_by_default():
    values = controller(profile(metrics={"rtt": options("rtt")}))
    assert set(values["anchors"]) == {"gnb", "upf"}
    assert values["rtt"]["enabled"]
    assert values["rtt"]["allowed"]
    assert "targets" not in values["rtt"]


@pytest.mark.parametrize("at", [["upf"], ["gnb"]])
def test_rtt_runs_only_where_at_says(at):
    values = controller(profile(metrics={"rtt": options("rtt", at=at)}))
    assert [target["role"] for target in values["rtt"]["targets"]] == at


def test_rtt_filters_reach_the_controller():
    rtt = options("rtt", imsis=["001010000000001"], slices=["01:ffffff"])
    values = controller(profile(metrics={"rtt": rtt}))
    assert values["rtt"]["imsis"] == ["001010000000001"]
    assert values["rtt"]["slices"] == ["01:ffffff"]


def test_packet_pairing_adds_the_pairer():
    values = controller(profile(metrics={"rtt": options("rtt", packet_pairing=True)}))
    assert values["rtt"]["pairer_image"]


def test_an_on_demand_metric_is_deployed_stopped():
    values = controller(profile(metrics={"throughput": options("throughput", on_demand=True)}))
    assert values["throughput"] == values["throughput"] | {"enabled": False, "allowed": True}


def test_each_upf_gets_its_own_throughput_probe():
    config = profile(
        metrics={"throughput": options("throughput")},
        targets=["amf", "smf", "upf", second_upf(), "gnb"],
        images={"controller": CONTROLLER},
    )
    values = controller(config, network(upfs=2))
    assert [target["component"] for target in values["throughput"]["targets"]] == ["upf", "upf2"]
    assert values["anchors"] == {}


def test_the_controller_watches_every_target_namespace():
    values = controller(profile(metrics={"rtt": options("rtt")}))
    assert values["targetNamespaces"] == ["core", "ran"]


# Host probes


def test_a_host_probe_watches_every_network_function_by_default():
    net = network()
    probe = manager(profile(metrics={"scheduler_wait": options("scheduler_wait")}), net).plan().components["probe-cpu"]
    watched = {(t["category"], t["component"]) for t in probe.chart_values["targets"]}
    model = net.deployment_model
    assert watched == {("core", name) for name in model.core.components} | {("ran", "gnb")}


def test_a_host_probe_can_be_limited_to_some_network_functions():
    config = profile(metrics={"scheduler_wait": options("scheduler_wait", targets=["ran/gnb"])})
    probe = manager(config).plan().components["probe-cpu"]
    assert [t["component"] for t in probe.chart_values["targets"]] == ["gnb"]


def test_a_host_probe_target_must_exist():
    config = profile(metrics={"scheduler_wait": options("scheduler_wait", targets=["ran/du"])})
    with pytest.raises(ValueError, match=r"Unresolved probe targets for cpu: \['ran/du'\]"):
        manager(config).plan()


def test_an_on_demand_host_probe_is_deployed_paused_with_no_pods_to_wait_for():
    config = profile(metrics={"scheduler_wait": options("scheduler_wait", on_demand=True)})
    plan = manager(config).plan()
    assert plan.components["probe-cpu"].chart_values["settings"]["paused"] is True
    assert plan.components["probe-cpu"].pod_selector == []
    assert plan.components["monitoring-controller"].chart_values["hostProbeStates"] == {"cpu": False}


@pytest.mark.parametrize("layout", ["cu-du", "cucp-cuup-du"])
def test_split_signaling_runs_on_a_split_ran(layout):
    config = profile(metrics={"split_signaling": options("split_signaling")}, targets=["amf", "smf", "upf"])
    assert "probe-split-signaling" in releases(config, network(layout=layout))


def test_split_signaling_is_refused_on_a_monolithic_gnb():
    config = profile(metrics={"split_signaling": options("split_signaling"), "rtt": options("rtt")})
    with pytest.raises(ValueError, match="split_signaling requires a modeled F1 or E1 split"):
        manager(config).plan()


# Logs


@pytest.mark.parametrize(
    ("logs", "namespaces"),
    [
        ({"core_logs": True}, "core"),
        ({"ran_logs": True}, "ran"),
        ({"ran_logs": True, "host_logs": True}, "monitoring|ran"),
    ],
)
def test_logs_are_kept_only_for_the_chosen_groups(logs, namespaces):
    promtail = manager(profile(metrics=logs)).plan().components["promtail"]
    assert promtail.chart_values["config"]["snippets"]["extraRelabelConfigs"][0]["regex"] == namespaces
