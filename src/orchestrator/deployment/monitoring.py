"""Monitoring deployment lifecycle and workload logs."""

from copy import deepcopy
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Literal

from orchestrator.deployment.helm import HelmManagement
from orchestrator.models.common import ComponentDependencyGraph
from orchestrator.models.monitoring import DeployedMonitoringComponent, MonitoringState
from orchestrator.models.provisioning import ProvisionableNode
from orchestrator.utils.ansible import get_ansible_inventory_line, run_ansible_role
from orchestrator.utils.monitoring import ASSETS, CHARTS, resolve_components, resolve_targets


class MonitoringManagement(HelmManagement):
    """Deploy and remove monitoring;"""

    # Choose the Kubernetes master, or the recorded installation node for existing monitoring.
    def _monitoring_control(self, *, installed: bool = False) -> tuple[str, ProvisionableNode]:
        monitoring = self.deployment_model.monitoring
        if installed:
            node_name = monitoring.state.installed_control_node
        else:
            node_name = self.deployment_model.params.k8s.master_node
        if not node_name or node_name not in self.deployment_model.nodes:
            raise ValueError("The Kubernetes master must be a node in the deployment model")
        return node_name, self.deployment_model.nodes[node_name]

    # Deployment lifecycle.

    # Resolve monitoring targets and assemble the Helm installation inputs.
    def plan(self) -> dict[str, Any]:
        """Validate and return the complete deployment plan without contacting a host."""
        monitoring = self.deployment_model.monitoring
        node_name, _ = self._monitoring_control()
        targets = resolve_targets(self.deployment_model)
        monitoring.components = resolve_components(self.deployment_model, targets)
        return {
            "control_node": node_name,
            "targets": targets,
            "variables": {
                "monitoring_namespace": monitoring.namespace,
                "monitoring_charts": str(CHARTS),
                "monitoring_node_name": monitoring.node_name or node_name,
                "monitoring_timeout_seconds": monitoring.operation_timeout_seconds,
            },
        }

    # Prepare chart files through the same asynchronous API as core and RAN.
    async def prepare(self, components: list[str] | None = None, *, quiet: bool = False) -> None:
        plan = self.plan()
        await self._run("prepare", variables=plan["variables"], quiet=quiet)

    # Standalone charts need deployment models, not synthetic 5G metamodel entries.
    def _get_helm_components(self, category):
        return None, self.deployment_model.monitoring.components

    # Reuse shared filtering, dependency scheduling and error handling.
    async def run_action(self, action, *, order: ComponentDependencyGraph | None = None, **kwargs):
        if action == "prepare":
            await self.prepare(components=kwargs.get("components"), quiet=kwargs.get("quiet", False))
            return None
        if action == "run_cmd":
            raise NotImplementedError("Monitoring command execution is not implemented")
        if order is None and action in ("install", "uninstall"):
            names = list(self.deployment_model.monitoring.components)
            if action == "uninstall":
                names.reverse()
            order = {}
            previous = None
            for name in names:
                order[name] = frozenset([previous]) if previous else frozenset()
                previous = name
        kwargs.setdefault("ignore_invalid_states", False)
        kwargs.setdefault("ignore_failures", False)
        kwargs.setdefault(
            "node",
            self._monitoring_control(installed=bool(self.deployment_model.monitoring.state.installed_namespace))[0],
        )
        return await super().run_action(category=None, action=action, order=order, **kwargs)

    # Adapt one shared Helm action to our chart roles and capture its installation state.
    async def _run_component_action(
        self,
        *,
        component_deployment,
        action,
        log_container=None,
        quiet=False,
        **kwargs,
    ) -> str | None:
        component = component_deployment
        output_keys = {
            "install": ["network_info"],
            "get_logs": ["logs"],
            "list_containers": ["container_names"],
        }.get(action)
        result = (
            await self._run(
                "remove" if action == "uninstall" else action,
                variables={
                    "component": component.model_dump(mode="json"),
                    "log_container": log_container,
                    "since_seconds": kwargs.get("since_seconds"),
                },
                output_keys=output_keys,
                installed=bool(self.deployment_model.monitoring.state.installed_namespace),
                quiet=quiet,
                node=kwargs.get("node_name"),
            )
            or {}
        )
        if action == "install":
            component.network_info = ip_address(result["network_info"])
            component.is_installed = True
        elif action == "uninstall":
            component.is_installed = False
            component.network_info = None
        elif output_keys:
            return result[output_keys[0]]
        return None

    # Execute a monitoring Ansible action using the selected control node.
    async def _run(
        self,
        action: Literal["prepare", "install", "capture", "wait_ready", "remove", "get_logs", "list_containers"],
        *,
        variables: dict[str, Any] | None = None,
        output_keys: list[str] | None = None,
        installed: bool = False,
        quiet: bool = False,
        node: str | None = None,
    ) -> dict[str, Any] | None:
        """Run a monitoring role action using the Kubernetes master's inventory."""
        monitoring = self.deployment_model.monitoring
        node_name, control_node = (
            self._get_deployment_node(node) if node else self._monitoring_control(installed=installed)
        )
        kubeconfig = monitoring.state.installed_kubeconfig if installed else monitoring.kubeconfig
        return await run_ansible_role(
            role="monitoring/stack",
            hosts=node_name,
            inventory=get_ansible_inventory_line(node_name, control_node),
            become=True,
            inputs={
                "monitoring_action": action,
                "monitoring_namespace": monitoring.state.installed_namespace if installed else monitoring.namespace,
                "monitoring_timeout_seconds": monitoring.operation_timeout_seconds,
                "monitoring_config": monitoring.model_dump(mode="json", exclude={"components"}),
            }
            | (variables or {}),
            output_keys=output_keys,
            extra_params={
                "name": "Monitoring",
                "environment": {"KUBECONFIG": kubeconfig},
                "module_defaults": {
                    "kubernetes.core.k8s": {"kubeconfig": kubeconfig},
                    "kubernetes.core.k8s_info": {"kubeconfig": kubeconfig},
                },
            },
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
            await self._run("prepare", variables=plan["variables"], quiet=quiet)
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
        await self._run("wait_ready", variables={"monitoring_components": components}, installed=True, quiet=quiet)

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

    # Select known component models without guessing identities from pod names or labels.
    def _selected_components(
        self, component: str | None, components: list[str] | None
    ) -> dict[str, DeployedMonitoringComponent]:
        if component is not None and components is not None:
            raise ValueError("Use component or components, not both")
        available = self.deployment_model.monitoring.components
        selected = [component] if component is not None else components
        if selected is None:
            return {name: model for name, model in available.items() if model.is_installed}
        unknown = set(selected) - available.keys()
        if unknown:
            raise ValueError(f"Unknown monitoring components: {sorted(unknown)}")
        return {name: available[name] for name in selected}

    # List regular, init and ephemeral containers using each component's pod selector.
    async def list_containers(
        self, component: str | None = None, *, components: list[str] | None = None, quiet: bool = False
    ) -> dict[str, list[str]]:
        return await super().list_containers(
            components=list(self._selected_components(component, components)),
            quiet=quiet,
        )

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
        return await super().get_logs(
            components=list(self._selected_components(component, components)),
            container=container,
            action_options={"since_seconds": since_seconds},
            quiet=quiet,
        )
