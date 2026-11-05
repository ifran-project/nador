"""Put the CSI output directory of each CSI radio node on a disk (live R2Lab nodes have their root in RAM)."""

import asyncio
from pathlib import Path

from orchestrator.deployment.base import BaseManagement
from orchestrator.utils.ansible import get_ansible_inventory_line, run_ansible_role

from fiveg_lens.csi.collection import ROLES

DEFAULT_STORAGE_MOUNT = "/mnt/csi-disk"


async def prepare_csi_storage(
    manager: BaseManagement,
    storage: dict[str, str] | None = None,
    *,
    storage_mount: str = DEFAULT_STORAGE_MOUNT,
    quiet: bool = True,
) -> dict[str, dict]:
    """
    Check, on the node of every CSI-enabled component, that its CSI directory is on a disk; run before ran.prepare().

    storage maps a node to its data disk partition under /dev (e.g. {"sopnode-f1": "sda1"}), as the storage
    variable of the 5g_ansible inventory. A node whose CSI directory is already on a disk (installed system,
    BI VM) needs no entry; a node whose directory is in RAM (live image) without an entry makes this fail.
    Returns, per node, the filesystem of the CSI directory before and after.
    """
    model = manager.deployment_model
    directories: dict[str, set[str]] = {}
    for name, component in model.ran.components.items():
        env = component.extra_env_vars or {}
        if str(env.get("CSI_LOGGING_ENABLED")) != "1":
            continue
        if not component.node_name or component.node_name not in model.nodes:
            raise ValueError(f"{name}: node_name must name a deployment node to prepare its CSI directory")
        directories.setdefault(component.node_name, set()).add(env.get("CSI_OUTPUT_DIR", "/data/csi"))
    if not directories:
        raise ValueError("No CSI-enabled component (see fiveg_lens.csi.enable_csi)")
    storage = storage or {}
    unknown = set(storage) - directories.keys()
    if unknown:
        raise ValueError(f"No CSI component on {sorted(unknown)}")

    ansible = model.params.ansible

    async def prepare(node_name: str, output_dir: str) -> dict:
        result = await run_ansible_role(
            role="csi/prepare_node",
            runner_path=str(Path(ansible.runner_path) / "csi" / "storage" / node_name),
            inputs={
                "csi_output_dir": output_dir,
                "csi_storage_device": storage.get(node_name, "").removeprefix("/dev/"),
                "csi_storage_mount": storage_mount,
            },
            output_keys=["csi_storage"],
            roles_paths=[str(ROLES), *ansible.roles_paths],
            hosts=node_name,
            inventory=get_ansible_inventory_line(node_name, model.nodes[node_name]),
            become=True,
            quiet=quiet,
        )
        return result["csi_storage"] | {"node": node_name, "directory": output_dir}

    jobs = [(node, directory) for node, dirs in directories.items() for directory in sorted(dirs)]
    reports = await asyncio.gather(*(prepare(node, directory) for node, directory in jobs))
    return {f"{node}:{directory}": report for (node, directory), report in zip(jobs, reports, strict=True)}
