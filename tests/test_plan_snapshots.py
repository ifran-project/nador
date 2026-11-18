"""Rendered Helm values for every example must match the reviewed snapshots.

Regenerate after an intended change with ``NADOR_UPDATE_SNAPSHOTS=1 pytest``
and review the diff like any other code change.
"""

import json
import os

import pytest
import yaml
from conftest import EXAMPLES, SNAPSHOTS, example_profiles, fake_model, implementation_for, load_example

from nador.planner import resolve_components, resolve_targets

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
    {"metrics": DEDICATED["metrics"] | {"split_signaling": True, "kernel_drops": True, "measurement_quality": True}},
)
CASES["variants/one-way-packet-pairing"] = (
    QUICKSTART,
    {
        "metrics": {
            "rtt": {"packet_pairing": True, "imsis": ["001010000000001"], "slices": ["01:ffffff"]},
            "n3_one_way": {"log_samples": True},
        },
        "images": {"one_way_probe": ONE_WAY},
    },
)
RFSIM = EXAMPLES / "monitoring" / "rfsim" / "oai-rfsim" / "monitoring.yaml"
CASES["variants/session-metrics"] = (
    RFSIM,
    {
        "operation_timeout_seconds": 600,
        "metrics": dict.fromkeys(("user_plane_paths", "ngap", "pfcp", "ue_mapper_coverage", "node_metrics"), True),
    },
)
CASES["variants/multi-upf-controller"] = (
    MULTI_UPF,
    {
        "metrics": dict.fromkeys(("ngap", "pfcp", "ue_mapper_coverage", "user_plane_paths", "node_metrics"), True)
        | {"n3_one_way": True},
        "images": {
            "controller": CONTROLLER,
            "one_way_probe": ONE_WAY,
            "ue_mapper": "ghcr.io/ifran-project/ue_mapper_api:v1",
        },
    },
)


def render(path, overlay) -> dict:
    """Plan an example offline and return a JSON-comparable result."""
    monitoring = load_example(path, overlay)
    model = fake_model(monitoring, implementation_for(path))
    targets = resolve_targets(model)
    components = resolve_components(model, targets)
    return {
        "targets": [target.values() for target in targets],
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
