"""configure_ran_monitoring refuses OCUDU log settings that would hide what monitoring reads from the logs."""

import pytest
from support import manager, network, options, profile

from nador import configure_ran_monitoring
from nador.config import validate_deployment

MAPPER = {"ran_kpis": options("ran_kpis")}  # the RNTI mapper is deployed with ran_kpis or csi
LOGS = {"ran_logs": True}


def ocudu(log=None, layout="gnb"):
    """An OCUDU RAN before ran.deploy(), its RRC node (gnb, or cucp) logging with ``log`` when given."""
    managed = manager(profile(), network(ran="ocudu", layout=layout))
    for component in managed.deployment_model.ran.components.values():
        component.is_installed = False
        if log is not None and component.advanced_config is not None:
            component.advanced_config = component.advanced_config.model_copy(update={"log": log})
    return managed


def configure(managed, **metrics):
    configure_ran_monitoring(managed, validate_deployment(profile(metrics=metrics), explicit=True))


@pytest.mark.parametrize(
    "log",
    [
        None,  # IFRAN's default: stdout, all_level info
        {"filename": "stdout", "all_level": "info", "phy_level": "warning"},
        {"filename": "stdout", "all_level": "warning", "cu_level": "info", "ngap_level": "debug"},
    ],
)
def test_log_settings_that_keep_the_ue_lines_are_accepted(log):
    configure(ocudu(log), **MAPPER, **LOGS)


@pytest.mark.parametrize(
    ("log", "problem"),
    [
        ({"all_level": "info"}, "gnb: log.filename must be stdout"),
        ({"filename": "stdout", "all_level": "warning"}, "gnb: log.cu_level must be info or debug"),
        ({"filename": "stdout", "all_level": "info", "ngap_level": "error"}, "gnb: log.ngap_level must be info"),
        ({"filename": "stdout", "all_level": "verbose"}, "gnb: unknown log level 'verbose' for cu"),
    ],
)
def test_log_settings_that_hide_the_ue_lines_are_refused_before_deploying(log, problem):
    with pytest.raises(ValueError, match=f"(?s)OCUDU log settings hide what monitoring reads.*{problem}"):
        configure(ocudu(log), **MAPPER)


def test_without_the_mapper_only_the_log_file_matters():
    quiet = {"filename": "stdout", "all_level": "warning"}
    configure(ocudu(quiet), **LOGS)  # no ran_kpis or csi: no RNTI mapper reading the CU lines
    with pytest.raises(ValueError, match="gnb: log.filename must be stdout"):
        configure(ocudu({"all_level": "info"}), **LOGS)


def test_nothing_is_checked_when_nothing_reads_the_ran_logs():
    configure(ocudu({"all_level": "none"}))


def test_in_a_split_ran_the_cu_cp_holds_the_ue_lines():
    with pytest.raises(ValueError, match="cucp: log.cu_level must be info"):
        configure(ocudu({"filename": "stdout", "all_level": "warning"}, layout="cucp-cuup-du"), **MAPPER)
