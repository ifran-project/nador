"""The OAI metrics parser image of NADOR's OAI chart values comes from images.json, rendered as IFRAN renders it."""

import json
import os
import shutil
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

import pytest

ANSIBLE = shutil.which("ansible", path=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
FALLBACK = (
    "ghcr.io/ifran-project/oai-gnb-metrics-parser"
    "@sha256:5ab00d04b270256eb2f80bf10a5091e6b2750e024adb2ea7579d7f043845d4e0"
)

pytestmark = [pytest.mark.integration, pytest.mark.skipif(ANSIBLE is None, reason="ansible is not installed")]


def parser_image(tmp_path: Path, pin: str | None) -> str:
    """Render the parser part of assets/ran/oai/values.yaml.j2 with Ansible's template module, next to images.json."""
    assets = tmp_path / "assets"
    shutil.copytree(Path(str(files("nador") / "assets")), assets)
    record = json.loads((assets / "images.json").read_text())
    record["images"].pop("oai_gnb_metrics_parser", None)
    if pin:
        record["images"]["oai_gnb_metrics_parser"] = pin
    (assets / "images.json").write_text(json.dumps(record))
    # The rest of the file includes IFRAN's own values, which only IFRAN's role can render.
    values = (assets / "ran" / "oai" / "values.yaml.j2").read_text()
    part = values[values.index("{# The OAI metrics parser") : values.index("{# CSI logger")]
    (assets / "ran" / "oai" / "part.j2").write_text(part)
    out = tmp_path / "values.yaml"
    subprocess.run(  # noqa: S603 - fixed arguments, the ansible of this environment
        [ANSIBLE, "localhost", "-c", "local", "-m", "ansible.builtin.template",
         "-a", f"src={assets / 'ran' / 'oai' / 'part.j2'} dest={out}"],
        check=True, capture_output=True, env=os.environ | {"ANSIBLE_LOCALHOST_WARNING": "False"},
    )  # fmt: skip
    (line,) = [line for line in out.read_text().splitlines() if line.strip().startswith("image:")]
    return line.split("image:", 1)[1].strip()


def test_the_parser_image_is_the_pin_of_images_json(tmp_path):
    pin = "ghcr.io/ifran-project/oai-gnb-metrics-parser:sha-abc1234@sha256:1111"
    assert parser_image(tmp_path, pin) == pin


def test_before_the_first_update_the_parser_is_the_published_image(tmp_path):
    assert parser_image(tmp_path, None) == FALLBACK
