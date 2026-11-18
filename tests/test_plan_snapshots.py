"""Rendered Helm values for every example must match the reviewed snapshots.

Regenerate after an intended change with ``NADOR_UPDATE_SNAPSHOTS=1 pytest``
and review the diff like any other code change.
"""

import json
import os

import pytest
import yaml
from conftest import EXAMPLES, SNAPSHOTS, example_profiles, fake_model, implementation_for, load_example

from nador.utils import resolve_components, resolve_targets

PROFILES = example_profiles()
DEDICATED = yaml.safe_load((EXAMPLES / "quickstart" / "dedicated-probes.yaml").read_text())
CASES = {**{name: (path, None) for name, path in PROFILES.items()}}
QUICKSTART = EXAMPLES / "quickstart" / "monitoring.yaml"
SPLIT = EXAMPLES / "monitoring" / "rfsim" / "oai-rfsim-split" / "monitoring.yaml"
MULTI_UPF = EXAMPLES / "monitoring" / "throughput" / "monitoring.yaml"
CONTROLLER = "ghcr.io/ifran-project/nador-controller:sha-75a58721962c2e237babeb0b95354bec22296616"
ONE_WAY = "ghcr.io/ifran-project/nador-n3-oneway:sha-75a58721962c2e237babeb0b95354bec22296616"
CASES["quickstart/dedicated-probes"] = (QUICKSTART, DEDICATED)
CASES["variants/split-signaling"] = (
    SPLIT,
    {"extended": DEDICATED["extended"] | {"split_signaling": True, "kernel_drops": True, "quality": True}},
)
CASES["variants/one-way-packet-pairing"] = (
    QUICKSTART,
    {
        "packet_pairing": True,
        "rtt_imsis": ["001010000000001"],
        "rtt_slices": ["01:ffffff"],
        "one_way": {"image": ONE_WAY, "n3": {"enabled": True, "log_samples": True}, "upf_transit": {"enabled": False}},
    },
)
CASES["variants/multi-upf-controller"] = (
    MULTI_UPF,
    {
        "probe_controller_image": CONTROLLER,
        "observability": {
            "ngap": True,
            "pfcp": True,
            "mapper_coverage": True,
            "user_plane_paths": True,
            "node_metrics": True,
            "mapper_image": "ghcr.io/ifran-project/ue_mapper_api:v1",
        },
        "one_way": {"image": ONE_WAY, "n3": {"enabled": True, "log_samples": False}, "upf_transit": {"enabled": False}},
    },
)


def render(path, overlay) -> dict:
    """Plan an example offline and return a JSON-comparable result."""
    monitoring = load_example(path, overlay)
    model = fake_model(monitoring, implementation_for(path))
    targets = resolve_targets(model)
    components = resolve_components(model, targets)
    return {
        "targets": targets,
        "components": {name: c.model_dump(mode="json") for name, c in components.items()},
    }


@pytest.mark.parametrize("name", sorted(CASES))
def test_rendered_values_match_snapshot(name):
    result = json.loads(json.dumps(render(*CASES[name]), sort_keys=True, default=str))
    snapshot = SNAPSHOTS / f"{name.replace('/', '__')}.json"
    if os.environ.get("NADOR_UPDATE_SNAPSHOTS"):
        snapshot.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    assert snapshot.exists(), f"Missing snapshot; run NADOR_UPDATE_SNAPSHOTS=1 pytest -k {name}"
    assert result == json.loads(snapshot.read_text())
