"""The monitoring profiles shipped with the examples stay valid as NADOR changes.

Users copy these files, so each must load under the same strict rules as their own. The test
only checks that they load: what a profile deploys is covered by the planner tests.
"""

from pathlib import Path

import pytest

from nador.config import MonitoringDeployment

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
# Profiles are named monitoring.yaml; notebooks save their deployment state beside them.
PROFILES = sorted(EXAMPLES.rglob("monitoring.yaml"))


def test_the_examples_ship_profiles():
    assert PROFILES


@pytest.mark.parametrize("path", PROFILES, ids=lambda path: str(path.relative_to(EXAMPLES)))
def test_an_example_profile_is_valid(path):
    MonitoringDeployment.from_yaml_file(path)
