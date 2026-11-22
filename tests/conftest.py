"""Fixtures shared by every test. Builders live in ``support.py``."""

import pytest
from support import Ansible

from nador import deployment


@pytest.fixture
def ansible(monkeypatch: pytest.MonkeyPatch) -> Ansible:
    """Record every Ansible role NADOR runs instead of reaching a cluster."""
    recorder = Ansible()
    monkeypatch.setattr(deployment, "run_ansible_role", recorder)
    return recorder
