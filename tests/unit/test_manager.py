"""The monitoring manager's lifecycle: deploy, change metrics while running, start, stop, remove.

Ansible is the only thing faked (the ``ansible`` fixture): each test checks which Helm releases
and control-node tasks NADOR asks for.
"""

import pytest
from support import manager, options, profile

SESSION = {"rtt": options("rtt")}


def release_names(managed):
    return [component.release_name for component in managed.deployment_model.monitoring.components.values()]


async def deployed(metrics=None, net=None):
    managed = manager(profile(metrics=metrics), net)
    await managed.deploy(quiet=True)
    return managed


# deploy and remove


async def test_deploy_installs_every_planned_release_then_starts_the_sniffers(ansible):
    managed = await deployed(SESSION)
    assert ansible.tasks() == ["prepare", "inject_sniffers"]
    assert ansible.releases("install") == release_names(managed)
    assert all(component.is_installed for component in managed.deployment_model.monitoring.components.values())


async def test_without_session_metrics_no_sniffers_are_injected(ansible):
    await deployed({"host_logs": True})
    assert ansible.tasks() == ["prepare"]


async def test_the_sniffers_export_only_what_the_profile_asks(ansible):
    await deployed(SESSION | {"ngap": options("ngap")})
    sniffers = next(c["sniffers"] for c in ansible.calls if c.get("monitoring_action") == "inject_sniffers")
    assert sniffers["amf"]["metrics_enabled"] is True
    assert sniffers["smf"]["metrics_enabled"] is False


async def test_remove_uninstalls_in_reverse_order(ansible):
    managed = await deployed(SESSION)
    ansible.clear()
    await managed.remove(quiet=True)
    assert ansible.releases("uninstall") == list(reversed(release_names(managed)))


async def test_an_invalid_profile_deploys_nothing(ansible):
    managed = manager(profile(metrics={"split_signaling": options("split_signaling"), **SESSION}))
    with pytest.raises(ValueError, match="split_signaling requires a modeled F1 or E1 split"):
        await managed.deploy(quiet=True)
    assert ansible.calls == []


# deploy_probe and remove_probe


async def test_a_host_probe_is_added_and_removed_alone(ansible):
    managed = await deployed({"host_logs": True})
    ansible.clear()
    await managed.deploy_probe("scheduler_wait", options("scheduler_wait"), quiet=True)
    assert ansible.helm() == [("install", "probe-cpu")]
    ansible.clear()
    await managed.remove_probe("scheduler_wait", quiet=True)
    assert ansible.helm() == [("uninstall", "probe-cpu")]


async def test_the_first_session_metric_brings_the_session_stack_and_the_sniffers(ansible):
    managed = await deployed({"host_logs": True})
    ansible.clear()
    await managed.deploy_probe("rtt", options("rtt"), quiet=True)
    stack = {"redis", "ue-mapper-api", "monitoring-controller"}
    assert set(ansible.releases("install")) == stack | {"monitoring-integration"}  # dashboards show RTT now
    assert ansible.tasks() == ["inject_sniffers"]


async def test_changing_a_running_metric_only_upgrades_the_controller(ansible):
    managed = await deployed(SESSION)
    ansible.clear()
    await managed.deploy_probe("rtt", options("rtt", at=["upf"]), quiet=True)
    assert ansible.helm() == [("install", "monitoring-controller")]
    assert ansible.tasks() == []


async def test_removing_the_last_session_metric_removes_the_session_stack(ansible):
    managed = await deployed(SESSION)
    ansible.clear()
    await managed.remove_probe("rtt", quiet=True)
    assert ansible.releases("uninstall") == ["monitoring-controller", "ue-mapper-api", "redis"]


async def test_a_failed_change_keeps_the_previous_profile_and_what_was_installed(ansible):
    managed = await deployed({"host_logs": True})
    ansible.clear()
    ansible.fail = ("install", "monitoring-controller")
    with pytest.raises(RuntimeError, match="helm install monitoring-controller failed"):
        await managed.deploy_probe("rtt", options("rtt"), quiet=True)
    monitoring = managed.deployment_model.monitoring
    assert monitoring.metrics.rtt is None
    installed = set(ansible.releases("install")) - {"monitoring-controller"}
    assert installed  # releases installed before the failure
    assert {c.release_name for c in monitoring.components.values() if c.is_installed} >= installed
    ansible.clear()
    await managed.remove(quiet=True)
    assert installed <= set(ansible.releases("uninstall"))


async def test_mistakes_change_nothing(ansible):
    managed = await deployed({"host_logs": True})
    before = managed.deployment_model.monitoring
    ansible.clear()
    with pytest.raises(ValueError, match="metrics.e2e.rtt: list every option"):
        await managed.deploy_probe("rtt", True)
    with pytest.raises(ValueError, match="ngap is not deployed"):
        await managed.remove_probe("ngap")
    with pytest.raises(ValueError, match="Unknown metric 'latency'"):
        await managed.deploy_probe("latency", True)
    with pytest.raises(ValueError, match="Use remove_probe"):
        await managed.deploy_probe("rtt", False)
    assert managed.deployment_model.monitoring is before
    assert ansible.calls == []


# start and stop


async def test_stop_and_start_patch_the_running_controller(ansible):
    managed = await deployed(SESSION)
    ansible.clear()
    await managed.stop("rtt", quiet=True)
    await managed.start("rtt", imsis=["001010000000001"], quiet=True)
    assert ansible.controller_settings() == [
        {"rtt": {"enabled": False}},
        {"rtt": {"enabled": True, "imsis": ["001010000000001"]}},
    ]
    assert ansible.helm() == []  # nothing is reinstalled
    assert managed.deployment_model.monitoring.metrics.rtt.imsis == ["001010000000001"]


async def test_start_can_move_rtt_to_one_anchor(ansible):
    managed = await deployed(SESSION)
    ansible.clear()
    await managed.start("rtt", at=["gnb"], quiet=True)
    (settings,) = ansible.controller_settings()
    assert [target["component"] for target in settings["rtt"]["targets"]] == ["gnb"]


async def test_stopping_a_host_probe_pauses_its_whole_probe(ansible):
    metrics = SESSION | {"kernel_drops": options("kernel_drops"), "attributed_drops": options("attributed_drops")}
    managed = await deployed(metrics)
    ansible.clear()
    await managed.stop("kernel_drops", quiet=True)
    assert ansible.controller_settings() == [{"host_probes": {"drops": False}}]
    assert managed.deployment_model.monitoring.metrics.attributed_drops.on_demand is True


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda m: m.start("ngap"), "ngap is not started or stopped by the controller"),
        (lambda m: m.start("throughput"), r"Deploy with metrics.core.throughput"),
        (lambda m: m.start("rtt", log_samples=True), "rtt does not take log_samples"),
        (lambda m: m.start("rtt", imsis=["12"]), "rtt: imsis: rtt imsis must contain quoted IMSIs"),
        (lambda m: m.start("rtt", at=["amf"]), "rtt: at: Input should be 'gnb' or 'upf'"),
    ],
)
async def test_start_refuses_what_the_deployment_cannot_run(ansible, call, message):
    managed = await deployed(SESSION)
    ansible.clear()
    with pytest.raises(ValueError, match=message):
        await call(managed)
    assert ansible.calls == []


# Workload logs


async def test_logs_are_read_from_installed_components_only(ansible):
    managed = await deployed({"host_logs": True})
    ansible.clear()
    logs = await managed.run_action("get_logs", quiet=True)
    assert set(logs) == {name for name, c in managed.deployment_model.monitoring.components.items() if c.pod_selector}
    with pytest.raises(ValueError, match="Unknown monitoring components"):
        await managed.run_action("get_logs", components=["nothing"])
