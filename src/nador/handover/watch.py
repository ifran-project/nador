"""Watch an F1 handover: note the RAN pods before it, then read the CU-CP and DU logs after it and analyze them."""

import json
import time

from ifran.deployment import ProvisioningManagement, RANManagement
from ifran.utils.k8s import build_k8s_safe

from nador.handover import oai, ocudu
from nador.handover.report import HandoverReport

__all__ = ["HandoverWatch"]

KUBECONFIG = "/etc/kubernetes/admin.conf"
# OCUDU logs are read from the start of the watch, with this margin; the OAI CU-CP log has no timestamps and is read
# from the trigger command on by the analysis itself.
LOG_MARGIN_S = 10
OAI_LOG_WINDOW_S = 3600


class HandoverWatch:
    """
    Check an F1 handover between the DUs ``source`` and ``target`` of the CU-CP ``cucp``, step by step, in the logs.

    NADOR does not trigger the handover, it observes it::

        watch = await HandoverWatch(ran, source="du1", target="du2").start()
        # trigger the handover: OAI ``ci trigger_f1_ho <CU UE ID>`` on the CU-CP telnet server, OCUDU
        # ``trigger_handover`` on the CU-CP remote control server (see nador.handover.oai and nador.handover.ocudu)
        report = await watch.report(ue)

    ``start`` records the restart count of every RAN container, so that ``report`` also reads the previous log of a DU
    container that restarted during the handover. ``ue`` is the CU UE ID with OAI, the C-RNTI in the source cell
    (``0x4601``) with OCUDU.
    """

    def __init__(self, ran: RANManagement, *, source: str, target: str, cucp: str = "cucp") -> None:
        model = ran.deployment_model
        unknown = [name for name in (cucp, source, target) if name not in model.ran.components]
        if unknown:
            raise ValueError(f"Unknown RAN components: {unknown}")
        implementation = str(model.ran.components[cucp].implementation)
        if implementation not in ("oai", "ocudu"):
            raise ValueError(f"handover of {implementation!r} RAN not supported (oai, ocudu)")
        self.ran = ran
        self.cucp, self.source, self.target = cucp, source, target
        self.implementation = implementation
        self.containers = (oai if implementation == "oai" else ocudu).CONTAINERS
        master, namespace = model.params.k8s.master_node, model.ran.components[target].namespace
        if master is None or namespace is None:
            raise ValueError("Deploy the RAN first: the Kubernetes master node and the RAN namespace are not known")
        self.master: str = master
        self.namespace: str = namespace
        self.nodes = ProvisioningManagement(preceding=ran)
        self.charts = {name: build_k8s_safe(f"{implementation}-{name}") for name in (cucp, source, target)}
        self._before: dict[str, dict[str, int]] | None = None
        self._started = 0.0

    async def start(self) -> "HandoverWatch":
        """Record the RAN containers before the handover; trigger it after this returns."""
        self._before = await self._pods()
        self._started = time.monotonic()
        return self

    async def report(
        self, ue: int | str, *, answer: str = "", quiet: bool = False, explain: bool = True
    ) -> HandoverReport:
        """
        Read the logs of the CU-CP and both DUs since ``start`` and check each step of the handover.

        ``answer`` is the CU-CP's answer to the trigger, when known. The report is printed unless quiet (with explain,
        what each step means and the log line that proves it), and returned.
        """
        if self._before is None:
            raise RuntimeError("Call start() before triggering the handover")
        after = await self._pods()
        names = [self.cucp, self.source, self.target]
        if self.implementation == "ocudu":
            since = int(time.monotonic() - self._started) + LOG_MARGIN_S
        else:
            since = OAI_LOG_WINDOW_S
        logs = await self.ran.get_logs(components=names, since_seconds=since, quiet=True, flatten=False)
        du_logs = {name: self._main_log(logs[name]) for name in (self.source, self.target)}  # type: ignore[index]
        restarts = await self._read_restarted(du_logs, self._before, after)
        cucp_log = self._main_log(logs[self.cucp])  # type: ignore[index]
        if self.implementation == "oai":
            report = oai.analyze_oai_handover(int(ue), self.source, self.target, cucp_log, du_logs)
        else:
            target = self.ran.metamodel.ran.components[self.target].config  # type: ignore[union-attr]
            report = ocudu.analyze_ocudu_handover(
                ue,
                self.source,
                self.target,
                cucp_log,
                du_logs,
                target_pci=getattr(target, "physicalCellId", None),
                answer=answer,
            )
        report.command_answer = answer
        report.restarts = restarts
        report.aftermath += [f"{name} restarted {count} time(s)" for name, count in restarts.items()]
        if not quiet:
            print(report.to_text(explain=explain))  # noqa: T201
        return report

    async def _kubectl(self, *args: str) -> str:
        output = await self.nodes.run_node_command(
            command=["kubectl", "--kubeconfig", KUBECONFIG, "-n", self.namespace, *args],
            nodes=[self.master],
            become=True,
            quiet=True,
        )
        return str(output)

    async def _pods(self) -> dict[str, dict[str, int]]:
        """Restart count of each container of each pod of the RAN namespace: {pod: {container: count}}."""
        pods = json.loads(await self._kubectl("get", "pods", "-o", "json"))["items"]
        return {
            pod["metadata"]["name"]: {c["name"]: c["restartCount"] for c in pod["status"].get("containerStatuses", [])}
            for pod in pods
        }

    def _main_log(self, containers: dict[str, str]) -> str:
        """Log of the container running the RAN function (the sidecars are left out)."""
        return "\n".join(log for name, log in containers.items() if name in self.containers)

    async def _read_restarted(
        self, du_logs: dict[str, str], before: dict[str, dict[str, int]], after: dict[str, dict[str, int]]
    ) -> dict[str, int]:
        """Restarts of the DU containers during the handover; their previous log is put before their current one."""
        restarts: dict[str, int] = {}
        for name in list(du_logs):
            for pod, containers in after.items():
                if not pod.startswith(self.charts[name] + "-"):
                    continue
                for container, count in containers.items():
                    increase = count - before.get(pod, {}).get(container, 0)
                    if container not in self.containers or increase <= 0:
                        continue
                    restarts[f"{name}/{container}"] = increase
                    # the handover happened in the previous container: read its log too
                    previous = await self._kubectl("logs", pod, "-c", container, "--previous")
                    du_logs[name] = previous + "\n" + du_logs[name]
        return restarts
