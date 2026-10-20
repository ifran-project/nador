"""Monitoring deployment lifecycle and workload logs."""

from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

from orchestrator.deployment.base import BaseManagement
from orchestrator.utils.ansible import get_ansible_inventory_line, run_ansible_role

from orchestrator_monitoring.models import MonitoringDeployment, MonitoringModel, MonitoringState
from orchestrator_monitoring.utils import (
    ASSETS,
    REMOTE_CHARTS,
    resolve_components,
    resolve_targets,
    run_monitoring_component_action,
)


class MonitoringManagement(BaseManagement):
    """Deploy monitoring components and attachment sniffers."""

    # Take an extension-owned snapshot of an existing orchestrator deployment.
    def __init__(self, preceding: BaseManagement, *, config: MonitoringDeployment | None = None):
        super().__init__(preceding=preceding)
        self.deployment_model = MonitoringModel.model_validate(self.deployment_model.model_dump(mode="python"))
        if config is not None:
            self.deployment_model.monitoring = config.model_copy(deep=True)

    # Use the Kubernetes controller, retaining its original location for removal.
    def _control_node(self):
        name = (
            self.deployment_model.monitoring.state.installed_control_node
            or self.deployment_model.params.k8s.master_node
        )
        if name is None:
            raise ValueError("A Kubernetes master node is required")
        return name, self.deployment_model.nodes[name]

    # Resolve monitoring targets and assemble the Helm installation inputs.
    def plan(self) -> dict[str, Any]:
        """Validate and return the complete deployment plan without contacting a host."""
        monitoring = self.deployment_model.monitoring
        node_name, _ = self._control_node()
        targets = resolve_targets(self.deployment_model)
        monitoring.components = resolve_components(self.deployment_model, targets)
        return {
            "control_node": node_name,
            "targets": targets,
        }

    # Prepare chart files through the same asynchronous API as core and RAN.
    async def prepare(self, components: list[str] | None = None, *, quiet: bool = False) -> None:
        self.plan()
        await self._run("prepare", quiet=quiet)

    # Run monitoring charts in installation order, reversing it for removal.
    async def run_action(
        self,
        action: str,
        *,
        components: list[str] | None = None,
        quiet: bool = False,
        container: str | None = None,
        since_seconds: int | None = None,
        command: list[str] | None = None,
        force: bool = True,
    ) -> dict[str, str]:
        cfg = self.deployment_model.monitoring
        names = list(cfg.components) if components is None else self._component_names(None, components)
        if action == "uninstall":
            names.reverse()
        node_name, node = self._control_node()
        output = {}
        for name in names:
            result = await run_monitoring_component_action(
                component_name=name,
                component_deployment=cfg.components[name],
                action=action,
                node_name=node_name,
                node_data=node,
                ansible_params=self.deployment_model.params.ansible,
                command_to_run=command,
                force=force,
                quiet=quiet,
                log_container=container,
                since_seconds=since_seconds,
                kubeconfig=cfg.state.installed_kubeconfig or cfg.kubeconfig,
                timeout_seconds=cfg.operation_timeout_seconds,
            )
            if result is not None:
                output[name] = result
        return output

    # Install the selected monitoring releases.
    async def install(self, components: list[str] | None = None, *, quiet: bool = False) -> None:
        await self.run_action("install", components=components, quiet=quiet)

    # Remove the selected monitoring releases in reverse installation order.
    async def uninstall(self, components: list[str] | None = None, *, quiet: bool = False) -> None:
        await self.run_action("uninstall", components=components, quiet=quiet)

    # Run a command in a monitoring component's first matching pod.
    async def run_command(self, command: list[str], *, components: list[str] | None = None, quiet: bool = False):
        return await self.run_action(
            "run_cmd", command=command, components=self._component_names(None, components), quiet=quiet
        )

    # Execute a monitoring Ansible action using the selected control node.
    async def _run(
        self,
        action: Literal["prepare", "capture", "wait_ready"],
        *,
        variables: dict[str, Any] | None = None,
        quiet: bool = False,
    ) -> None:
        """Run preparation or capture tasks using the Kubernetes master's inventory."""
        monitoring = self.deployment_model.monitoring
        node_name, control_node = self._control_node()
        await run_ansible_role(
            role="monitoring/stack",
            hosts=node_name,
            inventory=get_ansible_inventory_line(node_name, control_node),
            become=True,
            inputs={
                "monitoring_action": action,
                "monitoring_control_node": node_name,
                "monitoring_charts_directory": REMOTE_CHARTS,
                "monitoring_config": monitoring.model_dump(mode="json", exclude={"components"}),
            }
            | (variables or {}),
            extra_params={"name": "Monitoring"},
            runner_path=str(Path(self.deployment_model.params.ansible.runner_path) / "monitoring"),
            roles_paths=[str(ASSETS / "roles"), *self.deployment_model.params.ansible.roles_paths],
            quiet=quiet,
        )

    # Install monitoring and record its deployment status.
    async def deploy(self, *, quiet: bool = False) -> None:
        """Install monitoring and return only after workloads and attachment sniffers are ready."""
        monitoring = self.deployment_model.monitoring
        installed_state = monitoring.state
        if installed_state.installed_namespace:
            location_changed = (
                installed_state.installed_namespace != monitoring.namespace
                or installed_state.installed_control_node != self.deployment_model.params.k8s.master_node
                or installed_state.installed_kubeconfig != monitoring.kubeconfig
            )
            if location_changed:
                raise ValueError("Remove monitoring before changing its namespace, control node or kubeconfig")

        plan = self.plan()
        # Record the installation before starting so a partial deployment can be removed.
        monitoring.state = MonitoringState(
            status="deploying",
            installed_namespace=monitoring.namespace,
            installed_control_node=plan["control_node"],
            installed_kubeconfig=monitoring.kubeconfig,
            installed_targets=deepcopy(plan["targets"]),
            installed_latency=monitoring.latency,
        )
        try:
            await self._run("prepare", quiet=quiet)
            await self.install(quiet=quiet)
            if monitoring.latency:
                await self._run("capture", quiet=quiet)
            await self.wait_ready(quiet=quiet)
            monitoring.state.status = "deployed"
        except BaseException as error:
            monitoring.state.status = "failed"
            monitoring.state.last_error = str(error) or type(error).__name__
            raise

    # Wait for monitoring workloads and attachment sniffers to run.
    async def wait_ready(self, *, quiet: bool = False) -> None:
        """Wait for monitoring workloads; traffic metrics are verified separately."""
        monitoring = self.deployment_model.monitoring
        if not monitoring.state.installed_namespace:
            raise ValueError("No installed monitoring state; call deploy first")
        components = {
            name: component.model_dump(mode="json", exclude={"chart_values", "advanced_config"})
            for name, component in monitoring.components.items()
        }
        await self._run("wait_ready", variables={"monitoring_components": components}, quiet=quiet)

    # Remove components in reverse order, keeping services available during controller cleanup.
    async def remove(self, *, quiet: bool = False) -> None:
        """Remove component releases; retain state when cleanup fails."""
        monitoring = self.deployment_model.monitoring
        if not monitoring.state.installed_namespace:
            return
        monitoring.state.status = "removing"
        monitoring.state.last_error = None
        try:
            await self.uninstall(quiet=quiet)
        except BaseException as error:
            monitoring.state.status = "failed"
            monitoring.state.last_error = str(error) or type(error).__name__
            raise
        monitoring.state = MonitoringState()

    # Normalize the singular/plural selection arguments into known component names.
    def _component_names(self, component: str | None, components: list[str] | None) -> list[str]:
        if component is not None and components is not None:
            raise ValueError("Use component or components, not both")
        available = self.deployment_model.monitoring.components
        selected = [component] if component is not None else components
        if selected is None:
            return [name for name, model in available.items() if model.is_installed]
        unknown = set(selected) - available.keys()
        if unknown:
            raise ValueError(f"Unknown monitoring components: {sorted(unknown)}")
        return selected

    # Fetch one container's logs, or all containers, from explicitly selected components.
    async def get_logs(
        self,
        component: str | None = None,
        *,
        components: list[str] | None = None,
        container: str | None = None,
        since_seconds: int | None = None,
        quiet: bool = False,
    ) -> str | dict[str, str]:
        """Read component logs; return text for one component, otherwise a dictionary."""
        logs = await self.run_action(
            "get_logs",
            components=self._component_names(component, components),
            container=container,
            since_seconds=since_seconds,
            quiet=quiet,
        )
        return next(iter(logs.values())) if len(logs) == 1 else logs
