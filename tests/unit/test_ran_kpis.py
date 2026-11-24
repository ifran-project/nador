"""RAN KPIs: turning on OCUDU's native metrics stream, and finding every OCUDU component to read it from."""

import pytest
from support import manager, network, options, profile

from nador.config import validate_deployment
from nador.ran_kpis import configure_ran_kpis, ocudu_sources

KPIS = profile(metrics={"ran_kpis": options("ran_kpis")})


def ran(net):
    """A RAN manager as the notebooks hold it before ran.deploy(): the components are not installed yet."""
    for component in net.deployment_model.ran.components.values():
        component.advanced_config = None
    return manager(profile(), net)


def test_ocudu_components_get_their_metrics_stream_turned_on():
    managed = ran(network(ran="ocudu", layout="cucp-cuup-du"))
    configure_ran_kpis(managed, validate_deployment(KPIS))
    components = managed.deployment_model.ran.components
    du = components["du"].advanced_config
    assert du.metrics["enable_json"] is True
    assert du.metrics["layers"]["enable_sched"] is True
    assert du.remote_control == {"enabled": True, "bind_addr": "0.0.0.0", "port": 8001}
    assert set(components["cuup"].advanced_config.metrics["layers"]) == {"enable_app_usage", "enable_pdcp"}


def test_a_layer_turned_off_by_the_user_stays_off():
    managed = ran(network(ran="ocudu"))
    gnb = managed.deployment_model.ran.components["gnb"]
    gnb.advanced_config = (
        type(gnb)
        .model_validate(gnb.model_dump() | {"advanced_config": {"metrics": {"layers": {"enable_ru": False}}}})
        .advanced_config
    )
    configure_ran_kpis(managed, validate_deployment(KPIS))
    assert gnb.advanced_config.metrics["layers"]["enable_ru"] is False


def test_nothing_changes_without_ran_kpis_or_for_oai():
    for net, config in ((network(ran="ocudu"), profile()), (network(ran="oai"), KPIS)):
        managed = ran(net)
        configure_ran_kpis(managed, validate_deployment(config))
        assert managed.deployment_model.ran.components["gnb"].advanced_config is None


def test_a_deployed_ran_must_be_configured_before_it_was_deployed():
    managed = ran(network(ran="ocudu"))
    managed.deployment_model.ran.components["gnb"].is_installed = True
    with pytest.raises(ValueError, match="gnb: configure RAN KPIs before deploying the RAN"):
        configure_ran_kpis(managed, validate_deployment(KPIS))


def test_an_already_configured_deployed_ran_is_accepted():
    managed = ran(network(ran="ocudu"))
    configure_ran_kpis(managed, validate_deployment(KPIS))
    managed.deployment_model.ran.components["gnb"].is_installed = True
    configure_ran_kpis(managed, validate_deployment(KPIS))


# Sources the KPI exporter reads


def test_each_ocudu_component_is_a_source():
    model = manager(KPIS, network(ran="ocudu")).deployment_model
    model.monitoring = validate_deployment(KPIS)
    (source,) = ocudu_sources(model)
    assert source | {"component": "gnb", "namespace": "ran", "port": 8001, "selector": {"app": "ocudu-gnb"}} == source


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda gnb: setattr(gnb, "namespace", None), "gnb: RAN namespace is unresolved"),
        (lambda gnb: setattr(gnb, "advanced_config", None), r"call nador.configure_ran_monitoring\(ran, config\)"),
        (
            lambda gnb: gnb.advanced_config.remote_control.update(bind_addr="127.0.0.1"),
            "must bind to the pod network, not loopback",
        ),
    ],
)
def test_a_source_must_be_reachable(change, message):
    model = manager(KPIS, network(ran="ocudu")).deployment_model
    model.monitoring = validate_deployment(KPIS)
    change(model.ran.components["gnb"])
    with pytest.raises(ValueError, match=message):
        ocudu_sources(model)


def test_no_sources_without_ran_kpis():
    model = manager(profile(), network(ran="ocudu")).deployment_model
    assert ocudu_sources(model) == []
