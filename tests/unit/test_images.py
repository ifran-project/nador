"""Container images: references must be pinned, and the defaults must match their recorded builds."""

import json
from importlib.resources import files

import pytest

from nador.config import DEFAULT_IMAGES, is_pinned

RECORD = json.loads(files("nador").joinpath("assets/images.json").read_text())


@pytest.mark.parametrize(
    ("reference", "pinned"),
    [
        ("ghcr.io/x/y:v1", True),
        ("ghcr.io/x/y@sha256:abc", True),
        ("ghcr.io/x/y:v1@sha256:abc", True),
        ("registry:5000/x/y", False),
        ("ghcr.io/x/y", False),
        ("ghcr.io/x/y: v1", False),
        ("", False),
    ],
)
def test_a_reference_is_pinned_by_a_tag_or_a_digest(reference, pinned):
    assert is_pinned(reference) is pinned


@pytest.mark.parametrize("key", sorted(DEFAULT_IMAGES))
def test_every_default_image_is_pinned(key):
    assert is_pinned(DEFAULT_IMAGES[key])


@pytest.mark.parametrize("key", sorted(RECORD["images"]))
def test_every_default_image_is_a_recorded_build(key):
    commits = {build["digest"]: build["source_commit"] for build in RECORD["builds"].values()}
    name, _, digest = RECORD["images"][key].rpartition("@")
    assert digest in commits, f"{key}: digest is not in builds"
    tag = name.rsplit("/", 1)[-1].partition(":")[2]
    assert tag in ("", f"sha-{commits[digest]}"), f"{key}: tag does not name the build's commit"
