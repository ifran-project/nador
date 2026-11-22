"""Fixtures shared by every test. Builders live in ``support.py``."""

import pytest
from support import Ansible, HttpStub

from nador import deployment


@pytest.fixture
def ansible(monkeypatch: pytest.MonkeyPatch) -> Ansible:
    """Record every Ansible role NADOR runs instead of reaching a cluster."""
    recorder = Ansible()
    monkeypatch.setattr(deployment, "run_ansible_role", recorder)
    return recorder


@pytest.fixture
def http():
    """A local JSON server; set ``http.routes[path]`` to answer requests."""
    stub = HttpStub()
    yield stub
    stub.close()
