"""SSH from the notebook host to the nodes of the deployment model: commands and port forwards."""

import asyncio
import shlex
from pathlib import Path

from ifran.models.deployment import DeploymentModel
from ifran.models.provisioning import SSHInfo
from ifran.utils.ansible import get_ansible_inventory_line
from ifran.utils.utils import deep_merge

# A forward gets its own connection, without multiplexing: in ssh the first value of an option wins, so these
# come before the common arguments.
FORWARD_OPTIONS = ("ControlMaster=no", "ControlPersist=no", "ExitOnForwardFailure=yes")
OPTIONS = ("BatchMode=yes", "ServerAliveInterval=30", "ServerAliveCountMax=3")


def node_ssh(model: DeploymentModel, node: str) -> SSHInfo:
    """The SSH settings of a node: the deployment defaults (``params.ssh``) with the node's own settings on top.

    The same merge IFRAN does for its inventory lines, so NADOR reaches a node the way IFRAN does.
    """
    return SSHInfo.model_validate(
        deep_merge(
            model.params.ssh.model_dump(mode="json", exclude_unset=True),
            model.nodes[node].ssh_info.model_dump(mode="json", exclude_unset=True),
        )
    )


def inventory_line(model: DeploymentModel, node: str) -> str:
    """The Ansible inventory line of a node, with the deployment SSH defaults."""
    return get_ansible_inventory_line(node, model.nodes[node], model.params.ssh)


def ssh_command(ssh: SSHInfo, node: str, *remote: str, forward: str | None = None) -> list[str]:
    """The ssh argv to a node: run ``remote`` on it, or with ``forward`` (an ``-L`` spec) forward a port.

    ``ssh`` is the node's merged settings, from node_ssh().
    """
    command = ["ssh", "-T"]
    options = (*OPTIONS, f"ConnectTimeout={ssh.timeout}")
    if forward:
        command += ["-N", "-S", "none"]
        options = FORWARD_OPTIONS + options
    for option in options:
        command += ["-o", option]
    # The common arguments carry the jump host and the connection settings Ansible uses for the node.
    command += shlex.split(ssh.get_common_args() or "")
    if ssh.private_key_file:
        command += ["-i", str(Path(ssh.private_key_file).expanduser())]
    command += ["-p", str(ssh.port)]
    if forward:
        command += ["-L", forward]
    return [*command, f"{ssh.user}@{ssh.hostname or node}", *remote]


async def open_forward(
    command: list[str], local_port: int, *, name: str, log: Path | None = None, timeout_s: float = 20
) -> asyncio.subprocess.Process:
    """Start an ssh forward and return its process once 127.0.0.1:local_port accepts connections.

    ssh's errors go to ``log`` when given (kept after the call), else they are read when ssh exits.
    """
    if log is None:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
    else:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("w") as stderr:  # noqa: ASYNC230 - opened once, inherited by ssh
            process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.DEVNULL, stderr=stderr)
    try:
        async with asyncio.timeout(timeout_s):
            while process.returncode is None:
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", local_port)
                except OSError:
                    await asyncio.sleep(0.2)
                    continue
                writer.close()
                await writer.wait_closed()
                return process
    except BaseException:
        await close_forward(process)
        raise
    if log is None:
        error = (await process.communicate())[1].decode(errors="replace")
    else:
        error = log.read_text(errors="replace")  # noqa: ASYNC240 - a few lines written by ssh
    raise RuntimeError(f"{name} SSH tunnel failed: {error.strip()}")


async def close_forward(process: asyncio.subprocess.Process) -> None:
    """Stop an ssh forward: terminate it, and kill it if it does not exit within 5 s."""
    if process.returncode is not None:
        return
    try:
        process.terminate()
        await asyncio.wait_for(process.wait(), timeout=5)
    except ProcessLookupError:
        return
    except TimeoutError:
        process.kill()
        await process.wait()
