"""Offline fixtures: render monitoring plans for the example profiles without a cluster."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from nador.config import MonitoringDeployment, load_profile, validate_deployment

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
SNAPSHOTS = Path(__file__).with_name("snapshots")

# Component kinds inferred from the example component names.
KINDS = {"cuup": "cu_up", "cucp": "cu_cp", "cu": "cu", "gnb": "gnb", "du": "du"}


def example_profiles() -> dict[str, Path]:
    """Every complete monitoring profile shipped in examples/."""
    paths = sorted(EXAMPLES.rglob("monitoring*.yaml"))
    return {str(path.relative_to(EXAMPLES).with_suffix("")): path for path in paths}


def implementation_for(path: Path) -> str:
    """Infer the RAN implementation an example targets from its folder name."""
    text = str(path)
    return "ocudu" if "ocudu" in text or "r2lab" in text or "multi-gnb" in text else "oai"


def _kind(name: str) -> str:
    for prefix, kind in KINDS.items():
        if name.startswith(prefix):
            return kind
    raise ValueError(f"Cannot infer component kind for {name!r}")


def _component(implementation: str, kind: str, namespace: str) -> SimpleNamespace:
    advanced = None
    if implementation == "ocudu":
        advanced = SimpleNamespace(
            metrics={"enable_json": True},
            remote_control={"enabled": True, "bind_addr": "0.0.0.0", "port": 8001},  # noqa: S104
        )
    return SimpleNamespace(
        implementation=implementation,
        type=kind,
        namespace=namespace,
        advanced_config=advanced,
        is_installed=False,
    )


def fake_model(monitoring: MonitoringDeployment, ran_implementation: str) -> SimpleNamespace:
    """Build the parts of an IFRAN deployment model that planning reads."""
    core, ran = {}, {}
    for target in monitoring.targets:
        if target.category == "core":
            kind = target.role or target.component.rstrip("0123456789")
            core[target.component] = _component("open5gs", kind, "core")
        else:
            ran[target.component] = _component(ran_implementation, _kind(target.component), "ran")
    return SimpleNamespace(
        monitoring=monitoring,
        core=SimpleNamespace(implementation="open5gs", components=core),
        ran=SimpleNamespace(implementation=ran_implementation, components=ran),
        ues={},
    )


def load_example(path: Path, overlay: dict | None = None) -> MonitoringDeployment:
    """Read an example profile with the same strict loader users get."""
    if overlay is None:
        return MonitoringDeployment.from_yaml_file(path)
    data = load_profile(path)
    data.update(overlay)
    return validate_deployment(data, explicit=True)


@pytest.fixture
def quickstart() -> dict:
    """Raw quickstart profile as a dict, for tests that tweak single fields."""
    return yaml.safe_load((EXAMPLES / "quickstart" / "monitoring.yaml").read_text())
