"""First-class monitoring lifecycle using the library's Ansible execution layer."""

import asyncio
import hashlib
import json
import shlex
import socket
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from urllib.request import urlopen

from orchestrator.deployment.base import BaseManagement
from orchestrator.models.monitoring import MonitoringDeployment
from orchestrator.utils.ansible import get_ansible_inventory_line, run_ansible_command, run_ansible_playbook
from orchestrator.utils.monitoring import ASSETS, OWNER_LABEL, release_names, resolve_targets, resources


class MonitoringManagement(BaseManagement):
    """Deploy monitoring from the la<YOUR_PASSWORD> deployment model.

    prepare() is local and side-effect free. deploy()/wait_ready()/remove()
    execute against the explicitly selected cluster control node.
    """

    def _control(self, *, installed=False):
        cfg = self.deployment_model.monitoring
        name = (
            cfg.installed_control_node
            if installed
            else cfg.control_node or self.deployment_model.params.k8s.master_node
        )
        if not name or name not in self.deployment_model.nodes:
            raise ValueError("Set monitoring.control_node to a node in the deployment model")
        return name, self.deployment_model.nodes[name]

    def prepare(self) -> dict:
        """Validate and return the complete deployment plan without contacting a host."""
        cfg = MonitoringDeployment.model_validate(self.deployment_model.monitoring.model_dump())
        self.deployment_model.monitoring = cfg
        name, _ = self._control()
        targets = resolve_targets(self.deployment_model)
        manifests = resources(self.deployment_model, targets)
        for resource in manifests:
            if resource["kind"] == "Deployment":
                spec = resource["spec"]["template"]["spec"]
                spec["nodeSelector"] = {"post5g.blueprint/" + release_names(cfg.namespace)["stack"]: cfg.namespace}
                spec["tolerations"] = [
                    {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"},
                    {"key": "<YOUR_PASSWORD>-eviction", "operator": "Equal", "value": "true", "effect": "NoSchedule"},
                ]
        scrape_ns = sorted({cfg.namespace} | {t["namespace"] for t in targets})
        variables = {
            "monitoring_namespace": cfg.namespace,
            "monitoring_prometheus_stack_release": release_names(cfg.namespace)["stack"],
            "monitoring_grafana_release": release_names(cfg.namespace)["grafana"],
            "monitoring_loki_release": release_names(cfg.namespace)["loki"],
            "monitoring_prometheus_service_name": release_names(cfg.namespace)["stack"] + "-prometheus",
            "monitoring_loki_service_name": release_names(cfg.namespace)["loki"],
            "monitoring_node_name": cfg.node_name or name,
            "monitoring_node_label_key": "post5g.blueprint/" + release_names(cfg.namespace)["stack"],
            "monitoring_node_label_value": cfg.namespace,
            "monitoring_loki_enabled": cfg.loki,
            "monitoring_scrape_namespaces": scrape_ns,
            "monitoring_interval": cfg.interval,
            "monitoring_retention": cfg.retention,
            "monitoring_persistence_enabled": False,
            "monitoring_mount_storage_device": False,
            "monitoring_prometheus_node_port": cfg.prometheus_node_port,
            "monitoring_grafana_node_port": cfg.grafana_node_port,
            "monitoring_loki_node_port": cfg.loki_node_port,
            "ran": str(self.deployment_model.ran.implementation),
        }
        return {
            "control_node": name,
            "namespace": cfg.namespace,
            "targets": targets,
            "variables": variables,
            "manifests": manifests,
            "notes": [
                "Trial storage is ephemeral.",
                "The Grafana Helm chart manages its admin/admin credentials.",
                "Workload readiness does not prove that traffic metrics or UE mappings exist.",
            ],
        }

    def _play(self, tasks=None, roles=None, *, installed=False, variables=None):
        cfg = self.deployment_model.monitoring
        kubeconfig = cfg.installed_kubeconfig if installed else cfg.kubeconfig
        play = {
            "name": "Post-5G monitoring",
            "hosts": "monitoring_control",
            "gather_facts": False,
            "become": True,
            "environment": {"KUBECONFIG": kubeconfig},
            "module_defaults": {
                "kubernetes.core.k8s": {"kubeconfig": kubeconfig},
                "kubernetes.core.k8s_info": {"kubeconfig": kubeconfig},
            },
            "vars": {"monitoring_owner": cfg.installed_namespace if installed else cfg.namespace} | (variables or {}),
        }
        if tasks is not None:
            play["tasks"] = tasks
        if roles is not None:
            play["roles"] = roles
        return play

    async def _run(self, plays, quiet=False, *, installed=False):
        name, node = self._control(installed=installed)
        inventory = "[monitoring_control]\n" + get_ansible_inventory_line(name, node) + "\n"
        await run_ansible_playbook(
            playbook=plays,
            inventory=inventory,
            runner_path=str(Path(self.deployment_model.params.ansible.runner_path) / "monitoring"),
            roles_paths=[str(ASSETS / "roles"), *self.deployment_model.params.ansible.roles_paths],
            quiet=quiet,
        )

    @asynccontextmanager
    async def prometheus_tunnel(self):
        """Provide a verified local Prometheus URL for the duration of an experiment."""
        cfg = self.deployment_model.monitoring
        name, node = self._control(installed=bool(cfg.installed_namespace))
        ssh = node.ssh_info
        host = ssh.hostname or name
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        command = [
            "ssh",
            "-N",
            "-T",
            "-S",
            "none",
            "-o",
            "ControlMaster=no",
            "-o",
            "ControlPersist=no",
            "-o",
            "BatchMode=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
        ]
        command += shlex.split(ssh.common_args or "")
        if ssh.private_key_file:
            command += ["-i", str(Path(ssh.private_key_file).expanduser())]
        command += [
            "-p",
            str(ssh.port),
            "-L",
            f"127.0.0.1:{port}:{host}:{cfg.prometheus_node_port}",
            f"{ssh.user}@{host}",
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        url = f"http://127.0.0.1:{port}"

        def check_api():
            with urlopen(url + "/api/v1/query?query=up", timeout=1) as response:
                return json.load(response).get("status") == "success"

        try:
            try:
                async with asyncio.timeout(20):
                    while True:
                        if process.returncode is not None:
                            _, error = await process.communicate()
                            raise RuntimeError(
                                "Prometheus SSH tunnel failed: " + error.decode(errors="replace").strip()
                            )
                        try:
                            if await asyncio.to_thread(check_api):
                                break
                        except (OSError, ValueError):
                            pass
                        await asyncio.sleep(0.2)
            except TimeoutError as exc:
                raise RuntimeError(
                    "Prometheus API is unreachable through the SSH tunnel; check the VM and monitoring service"
                ) from exc
            yield url
        finally:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await process.wait()

    async def _command(self, command, quiet):
        cfg = self.deployment_model.monitoring
        installed = bool(cfg.installed_namespace)
        name, node = self._control(installed=installed)
        return await run_ansible_command(
            command=[
                "kubectl",
                "--kubeconfig",
                cfg.installed_kubeconfig or cfg.kubeconfig,
                "-n",
                cfg.installed_namespace or cfg.namespace,
                *command,
            ],
            hosts=name,
            inventory=get_ansible_inventory_line(name, node),
            become=True,
            quiet=quiet,
            runner_path=str(Path(self.deployment_model.params.ansible.runner_path) / "monitoring"),
        )

    async def _pods(self, component, components, quiet):
        if component is not None and components is not None:
            raise ValueError("Use component or components, not both")
        selected = [component] if component is not None else components
        pods = json.loads(await self._command(["get", "pods", "-o", "json"], quiet))["items"]
        groups = {}
        for pod in pods:
            meta = pod["metadata"]
            owner = next((o for o in meta.get("ownerReferences", []) if o.get("controller")), None)
            workload = owner["name"] if owner else meta["name"]
            if owner and owner["kind"] == "ReplicaSet":
                workload = workload.rsplit("-", 1)[0]
            labels = meta.get("labels", {})
            aliases = {
                meta["name"],
                workload,
                labels.get("app"),
                labels.get("app.kubernetes.io/name"),
                labels.get("app.kubernetes.io/instance"),
            }
            for key in [workload] if selected is None else [n for n in selected if n in aliases]:
                groups.setdefault(key, []).append(pod)
        for name in selected or []:
            if name not in groups:
                raise ValueError(f"No monitoring pods found for {name!r}")
        return groups

    async def list_containers(
        self, component: str | None = None, *, components: list[str] | None = None, quiet: bool = False
    ) -> dict[str, list[str]]:
        """List regular, init and ephemeral containers, grouped by workload."""
        pods = await self._pods(component, components, quiet)
        return {
            name: sorted(
                {
                    c["name"]
                    for pod in group
                    for kind in ("containers", "initContainers", "ephemeralContainers")
                    for c in pod["spec"].get(kind, [])
                }
            )
            for name, group in pods.items()
        }

    async def get_logs(
        self,
        component: str | None = None,
        *,
        components: list[str] | None = None,
        container: str | None = None,
        since_seconds: int = 3600,
        quiet: bool = False,
    ) -> str | dict[str, str]:
        """Read monitoring logs; one workload returns text, multiple return a dictionary.

        Select a workload name, pod name or app label. Omit container to fetch all
        containers, and omit component/components to fetch all monitoring workloads.
        """
        if since_seconds < 0:
            raise ValueError("since_seconds must be non-negative")
        pods = await self._pods(component, components, quiet)
        logs = {}
        for name, group in pods.items():
            output = []
            for pod in group:
                if container is not None and container not in {
                    c["name"]
                    for kind in ("containers", "initContainers", "ephemeralContainers")
                    for c in pod["spec"].get(kind, [])
                }:
                    raise ValueError(f"Container {container!r} not found in {pod['metadata']['name']!r}")
                command = ["logs", pod["metadata"]["name"], f"--since={since_seconds}s"]
                command += ["-c", container] if container is not None else ["--all-containers=true"]
                if container is None or len(group) > 1:
                    command += ["--prefix=true"]
                output.append(await self._command(command, quiet))
            logs[name] = "\n".join(output)
        return next(iter(logs.values())) if len(logs) == 1 else logs

    def _preflight(self, plan):
        cfg = self.deployment_model.monitoring
        tasks = [
            {
                "name": "Verify scheduling node",
                "kubernetes.core.k8s_info": {
                    "api_version": "v1",
                    "kind": "Node",
                    "name": plan["variables"]["monitoring_node_name"],
                },
                "register": "monitoring_node",
            },
            {
                "name": "Require a real Kubernetes node",
                "ansible.builtin.assert": {"that": ["monitoring_node.resources | length == 1"]},
            },
        ]
        if not cfg.installed_namespace:
            tasks += [
                {
                    "name": "Check trial namespace is unused by Helm",
                    "ansible.builtin.command": {
                        "argv": [
                            "helm",
                            "list",
                            "--deployed",
                            "--failed",
                            "--pending",
                            "--uninstalled",
                            "--uninstalling",
                            "--superseded",
                            "-n",
                            cfg.namespace,
                            "-o",
                            "json",
                        ]
                    },
                    "register": "existing_releases",
                    "changed_when": False,
                },
                {
                    "name": "Require a separate trial namespace",
                    "ansible.builtin.assert": {
                        "that": ["existing_releases.stdout | from_json | length == 0"],
                        "fail_msg": "Use a namespace without existing Helm releases, or restore saved monitoring state.",
                    },
                },
            ]
        tasks += [
            {
                "name": "Check configured packet and metric targets",
                "ansible.builtin.include_tasks": str(ASSETS / "preflight_target.yml"),
                "loop": plan["targets"],
                "loop_control": {"loop_var": "monitoring_target", "label": "{{ monitoring_target.component }}"},
            }
        ]
        tasks += [
            {
                "name": "Create monitoring namespace",
                "kubernetes.core.k8s": {
                    "state": "present",
                    "definition": {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": cfg.namespace}},
                },
            },
        ]
        return self._play(tasks)

    async def deploy(self, *, quiet=False):
        """Install the stack and model-selected addons, then check rollout readiness."""
        plan = self.prepare()
        cfg = self.deployment_model.monitoring

        def identities(manifests):
            return {
                (d["apiVersion"], d["kind"], d["metadata"].get("namespace"), d["metadata"]["name"]) for d in manifests
            }

        if cfg.installed_namespace and (
            cfg.installed_namespace != cfg.namespace
            or cfg.installed_control_node != plan["control_node"]
            or cfg.installed_kubeconfig != cfg.kubeconfig
            or cfg.installed_targets != plan["targets"]
            or cfg.installed_loki != cfg.loki
            or identities(cfg.installed_manifests) != identities(plan["manifests"])
        ):
            raise ValueError("Remove the installed monitoring profile before changing its resources or targets")
        await self._run([self._preflight(plan)], quiet)
        # Record ownership before side effects so removal also works after partial failures.
        cfg.installed_releases = release_names(cfg.namespace)
        cfg.installed_namespace = cfg.namespace
        cfg.installed_control_node = plan["control_node"]
        cfg.installed_kubeconfig = cfg.kubeconfig
        cfg.installed_targets = deepcopy(plan["targets"])
        cfg.installed_manifests = deepcopy(plan["manifests"])
        cfg.installed_loki = cfg.loki
        cfg.status, cfg.last_error = "deploying", None
        try:
            await self._run([self._play(roles=["monitoring/stack"], variables=plan["variables"])], quiet)
            if plan["manifests"]:
                await self._run(
                    [
                        self._play(
                            [
                                {
                                    "name": "Apply monitoring addons",
                                    "kubernetes.core.k8s": {"state": "present", "definition": plan["manifests"]},
                                }
                            ]
                        )
                    ],
                    quiet,
                )
            await self.wait_ready(quiet=quiet)
            cfg.status = "deployed"
        except BaseException as exc:
            cfg.status, cfg.last_error = "failed", str(exc) or type(exc).__name__
            raise

    async def wait_ready(self, *, quiet=False):
        """Wait for deployments and Helm stack pods; metric availability is separate."""
        cfg = self.deployment_model.monitoring
        if not cfg.installed_namespace:
            raise ValueError("No installed monitoring state; call deploy first")
        ns = cfg.installed_namespace
        tasks = []
        for label in ["app.kubernetes.io/name=prometheus", "app.kubernetes.io/name=grafana"]:
            tasks.append(
                {
                    "name": f"Wait for {label}",
                    "ansible.builtin.command": {
                        "argv": [
                            "kubectl",
                            "-n",
                            ns,
                            "wait",
                            "--for=condition=Ready",
                            "pod",
                            "-l",
                            label,
                            f"--timeout={cfg.timeout_seconds}s",
                        ]
                    },
                    "changed_when": False,
                }
            )
        for d in cfg.installed_manifests:
            if d["kind"] == "Deployment":
                tasks.append(
                    {
                        "name": f"Wait for {d['metadata']['name']}",
                        "ansible.builtin.command": {
                            "argv": [
                                "kubectl",
                                "-n",
                                d["metadata"]["namespace"],
                                "rollout",
                                "status",
                                "deployment/" + d["metadata"]["name"],
                                f"--timeout={cfg.timeout_seconds}s",
                            ]
                        },
                        "changed_when": False,
                    }
                )
        await self._run([self._play(tasks, installed=True)], quiet, installed=True)

    async def wait_capture_ready(self, *, quiet=False):
        """Wait for owned AMF/SMF capture containers before attaching UEs.

        Running containers are a prerequisite, not proof of decoded traffic.
        Tunnel-dependent latency probes are checked after UE attachment.
        """
        cfg = self.deployment_model.monitoring
        if cfg.status != "deployed" or not cfg.installed_namespace:
            raise ValueError("Deploy monitoring before waiting for packet capture")
        if not cfg.latency:
            raise ValueError("Enable latency monitoring to capture UE attachment")
        tasks = []
        for target in cfg.installed_targets:
            if target["role"] not in ("amf", "smf"):
                continue
            prefix = "post5g-" + target["role"] + "-sniffer-"
            tasks.append(
                {
                    "name": "Wait for attachment capture on " + target["component"],
                    "kubernetes.core.k8s_info": {
                        "api_version": "v1",
                        "kind": "Pod",
                        "namespace": target["namespace"],
                        "label_selectors": target["label_selectors"],
                    },
                    "register": "capture_pods",
                    "until": [
                        "capture_pods.resources | length == 1",
                        "(capture_pods.resources | first | default({})).get('metadata', {}).get('annotations', {}).get('post5g.blueprint/packet-owner', '') == monitoring_owner",
                        "(capture_pods.resources | first | default({})).get('status', {}).get('ephemeralContainerStatuses', []) | selectattr('name', 'match', '^"
                        + prefix
                        + "') | selectattr('state.running', 'defined') | list | length > 0",
                    ],
                    "retries": max(1, cfg.timeout_seconds // 2),
                    "delay": 2,
                }
            )
        if len(tasks) != 2:
            raise ValueError("Installed monitoring must contain AMF and SMF capture targets")
        await self._run([self._play(tasks, installed=True)], quiet, installed=True)

    async def remove(self, *, quiet=False):
        """Stop injected processes before removing owned monitoring resources.

        Namespace, CRDs and network-function pods are retained.
        Failure leaves installed state available for a retry.
        """
        cfg = self.deployment_model.monitoring
        if not cfg.installed_namespace:
            return
        cfg.status, cfg.last_error = "removing", None
        ns = cfg.installed_namespace
        tasks = []
        controllers = [
            d
            for d in cfg.installed_manifests
            if d["kind"] == "Deployment" and d["metadata"]["name"] == "post5g-monitoring-controller"
        ]
        if controllers:
            controller = controllers[0]
            # Apply the code/SA/RBAC needed by cleanup even after a partially failed deployment.
            prerequisites = [
                d
                for d in cfg.installed_manifests
                if d["kind"] in ("ConfigMap", "ServiceAccount", "Role", "RoleBinding")
            ]
            tasks += [
                {
                    "name": "Prepare cleanup permissions and code",
                    "kubernetes.core.k8s": {"state": "present", "definition": prerequisites},
                },
                {
                    "name": "Stop the monitoring reconciler",
                    "kubernetes.core.k8s": {
                        "state": "present",
                        "definition": deepcopy(controller) | {"spec": controller["spec"] | {"replicas": 0}},
                    },
                },
                {
                    "name": "Wait for reconciler shutdown",
                    "ansible.builtin.command": {
                        "argv": [
                            "kubectl",
                            "-n",
                            ns,
                            "get",
                            "pod",
                            "-l",
                            "app=post5g-monitoring-controller",
                            "-o",
                            "json",
                        ]
                    },
                    "register": "monitoring_stopped",
                    "until": "(monitoring_stopped.stdout | from_json)['items'] | length == 0",
                    "retries": max(1, cfg.timeout_seconds // 2),
                    "delay": 2,
                    "changed_when": False,
                },
            ]
            pod = deepcopy(controller["spec"]["template"])
            pod["spec"]["restartPolicy"] = "Never"
            pod["spec"]["containers"][0]["command"] = ["python", "/post5g/controller.py", "--cleanup"]
            pod["metadata"]["labels"]["app"] = "post5g-monitoring-cleanup"
            job = {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"name": "post5g-monitoring-cleanup", "namespace": ns},
                "spec": {"backoffLimit": 0, "activeDeadlineSeconds": cfg.timeout_seconds, "template": pod},
            }
            tasks += [
                {
                    "name": "Remove an earlier cleanup job",
                    "kubernetes.core.k8s": {
                        "state": "absent",
                        "api_version": "batch/v1",
                        "kind": "Job",
                        "name": "post5g-monitoring-cleanup",
                        "namespace": ns,
                        "wait": True,
                    },
                },
                {
                    "name": "Stop owned sniffers and latency probes",
                    "kubernetes.core.k8s": {"state": "present", "definition": job},
                },
                {
                    "name": "Require successful packet cleanup",
                    "ansible.builtin.command": {
                        "argv": [
                            "kubectl",
                            "-n",
                            ns,
                            "wait",
                            "--for=condition=complete",
                            "job/post5g-monitoring-cleanup",
                            f"--timeout={cfg.timeout_seconds}s",
                        ]
                    },
                    "changed_when": False,
                },
            ]
        if cfg.installed_manifests:
            # Keep RBAC and source until the cleanup job has finished.
            tasks.append(
                {
                    "name": "Remove owned addon resources",
                    "kubernetes.core.k8s": {
                        "state": "absent",
                        "definition": list(reversed(cfg.installed_manifests)),
                        "wait": True,
                    },
                }
            )
        if controllers:
            tasks += [
                {
                    "name": "Remove shared packet pairer",
                    "kubernetes.core.k8s": {
                        "state": "absent",
                        "api_version": api,
                        "kind": kind,
                        "name": name,
                        "namespace": ns,
                    },
                }
                for api, kind, name in [
                    ("apps/v1", "Deployment", "post5g-packet-pairer"),
                    ("v1", "Service", "post5g-packet-pairer"),
                    ("batch/v1", "Job", "post5g-monitoring-cleanup"),
                ]
            ]
        # Older saved states predate explicit release names and used namespace hashes.
        prefix = "p5g-" + hashlib.sha256(ns.encode()).hexdigest()[:10]
        releases = cfg.installed_releases or {
            "stack": prefix + "-prom",
            "grafana": prefix + "-grafana",
            "loki": prefix + "-loki",
        }
        for release in [releases["grafana"], *([releases["loki"]] if cfg.installed_loki else []), releases["stack"]]:
            tasks.append(
                {
                    "name": f"Remove {release}",
                    "kubernetes.core.helm": {"name": release, "release_namespace": ns, "state": "absent", "wait": True},
                }
            )
        tasks.append(
            {
                "name": "Remove dashboard ConfigMap",
                "kubernetes.core.k8s": {
                    "state": "absent",
                    "kind": "ConfigMap",
                    "api_version": "v1",
                    "namespace": ns,
                    "name": "monitoring-dashboard-configmap",
                },
            }
        )
        try:
            await self._run([self._play(tasks, installed=True)], quiet, installed=True)
        except BaseException as exc:
            cfg.status, cfg.last_error = "failed", str(exc) or type(exc).__name__
            raise
        cfg.status = "not_deployed"
        cfg.installed_releases = {}
        cfg.installed_manifests, cfg.installed_targets = [], []
        cfg.installed_namespace = cfg.installed_control_node = cfg.installed_kubeconfig = None
        cfg.installed_loki = False
