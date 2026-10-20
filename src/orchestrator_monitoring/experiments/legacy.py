"""Deliberately narrow bridge to a reviewed 5g_ansible develop checkout."""
from pathlib import Path
from typing import Any

from orchestrator.deployment.base import BaseManagement
from orchestrator.deployment.miscellaneous import Miscellaneous


async def run_legacy_experiment(stage: BaseManagement, *, legacy_root: str | Path,
                                scenario: str | Path, inventory: str | Path,
                                variables: dict[str, Any], artifact_profile: str | Path | None = None,
                                quiet: bool = False) -> None:
    """Run the old generic experiment runner using explicit legacy configuration.

    Complex scenarios retain their own requirements; this does not silently
    infer old inventory groups, physical-UE interfaces or namespace semantics.
    The root must be a reviewed develop checkout/snapshot with its dependencies.
    """
    root = Path(legacy_root).expanduser().resolve()
    inventory = Path(inventory).expanduser().resolve()
    scenario = (root / scenario).resolve()
    playbook = root/'playbooks/run_experiment.yml'
    for path in [inventory, scenario, playbook]:
        if not path.is_file():
            raise FileNotFoundError(path)
    if not isinstance(variables.get('core'), str) or not isinstance(variables.get('ran'), str):
        raise ValueError('Supply explicit legacy core and ran strings; model dictionaries are incompatible')
    inputs = dict(variables) | {
        'experiment_inventory': str(inventory), 'experiment_scenario_file': str(scenario),
    }
    if artifact_profile:
        profile = (root / artifact_profile).resolve()
        if not profile.is_file():
            raise FileNotFoundError(profile)
        inputs['experiment_artifacts_file'] = str(profile)
    bridge = Miscellaneous(preceding=stage)
    await bridge.run_playbook(playbook=str(playbook), inventory=str(inventory),
        inputs=inputs, roles_paths=[str(root/'roles')], store=False, quiet=quiet)
