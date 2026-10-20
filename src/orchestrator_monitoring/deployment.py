"""Monitoring deployment lifecycle and workload logs."""

from pathlib import Path
from typing import Any, Literal

from orchestrator.deployment.base import BaseManagement
from orchestrator.deployment.helm import HelmManagement
from orchestrator.utils.ansible import get_ansible_inventory_line, run_ansible_role

from orchestrator_monitoring.models import MonitoringDeployment, MonitoringModel
from orchestrator_monitoring.utils import (
    ASSETS,
    resolve_components,
    resolve_targets,
    run_monitoring_component_action,
)


class MonitoringManagement(HelmManagement):
    """Deploy monitoring components and attachment sniffers."""

    # Take an extension-owned snapshot of an existing orchestrator deployment.
    def __init__(self, preceding: BaseManagement, *, config: MonitoringDeployment | None = None):
        super().__init__(preceding=preceding)
        self.deployment_model = MonitoringModel.model_validate(self.deployment_model.model_dump(mode="python"))
        if config is not None:
            self.deployment_model.monitoring = config.model_copy(deep=True)

    # Resolve monitoring targets and assemble the Helm installation inputs.
    def plan(self) -> dict[str, Any]:
        """Validate and return the complete deployment plan without contacting a host."""
        monitoring = self.deployment_model.monitoring
        node_name, _ = self._get_deployment_node(None)
        targets = resolve_targets(self.deployment_model)
        monitoring.components = resolve_components(self.deployment_model, targets)
        return {
            "control_node": node_name,
            "targets": targets,
        }

    # Prepare chart files through the same asynchronous API as core and RAN.
    async def prepare(self, *, quiet: bool = False) -> None:
        self.plan()
        await self._run("prepare", quiet=quiet)

    # Run monitoring charts in installation order, reversing it for removal.
    async def run_action(
        self,
        action: str,
        *,
        components: list[str] | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        cfg = self.deployment_model.monitoring
        names = list(cfg.components) if components is None else list(components)
        unknown = set(names) - cfg.components.keys()
        if unknown:
            raise ValueError(f"Unknown monitoring components: {sorted(unknown)}")
        if components is None and action in ("get_logs", "run_cmd"):
            names = [name for name in names if cfg.components[name].is_installed and cfg.components[name].pod_selector]
        if action == "uninstall":
            names.reverse()
        node = self._get_deployment_node(None)
        output = {}
        for name in names:
            result = await run_monitoring_component_action(
                model=self.deployment_model,
                component=cfg.components[name],
                component_name=name,
                node=node,
                action=action,
                **kwargs,
            )
            if result is not None:
                output[name] = result
        return output

    # Execute a monitoring Ansible action using the selected control node.
    async def _run(
        self,
        action: Literal["prepare", "capture"],
        *,
        targets: list[dict] | None = None,
        quiet: bool = False,
    ) -> None:
        """Run preparation or capture tasks using the Kubernetes master's inventory."""
        monitoring = self.deployment_model.monitoring
        node_name, control_node = self._get_deployment_node(None)
        await run_ansible_role(
            role="monitoring/stack",
            hosts=node_name,
            inventory=get_ansible_inventory_line(node_name, control_node),
            become=True,
            inputs={
                "monitoring_action": action,
                "monitoring_control_node": node_name,
                "monitoring_config": monitoring.model_dump(mode="json", exclude={"components"}),
                "monitoring_targets": targets or [],
            },
            extra_params={"name": "Monitoring"},
            runner_path=str(Path(self.deployment_model.params.ansible.runner_path) / "monitoring"),
            roles_paths=[str(ASSETS / "roles"), *self.deployment_model.params.ansible.roles_paths],
            quiet=quiet,
        )

    # Install monitoring components, then inject and start attachment sniffers.
    async def deploy(self, *, quiet: bool = False) -> None:
        """Deploy monitoring and wait for its components and sniffers."""
        plan = self.plan()
        await self._run("prepare", quiet=quiet)
        await self.install(quiet=quiet)
        if self.deployment_model.monitoring.latency:
            await self._run("capture", targets=plan["targets"], quiet=quiet)

    # Uninstall monitoring releases in reverse installation order.
    async def remove(self, *, quiet: bool = False) -> None:
        """Remove monitoring using the current deployment configuration."""
        await self.uninstall(quiet=quiet)
