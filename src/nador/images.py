"""Container image references shared by every settings model."""

from typing import Annotated

from pydantic import AfterValidator


def is_pinned(reference: str) -> bool:
    """Whether a reference names a tag or digest (``repo:tag``, ``repo@sha256:...``)."""
    return ":" in reference.rsplit("/", 1)[-1] and not any(c.isspace() for c in reference)


def _require_pinned(reference: str) -> str:
    if not is_pinned(reference):
        raise ValueError("must be a tagged image (repo:tag) or digest (repo@sha256:...)")
    return reference


TaggedImage = Annotated[str, AfterValidator(_require_pinned)]
"""An image reference that never falls back to an implicit ``latest`` tag."""


def split_image(reference: str) -> tuple[str, str]:
    """Split ``repo:tag[@digest]`` into repository and tag, keeping any digest with the tag."""
    name, at, digest = reference.partition("@")
    repository, colon, tag = name.rpartition(":")
    if not colon or "/" in tag:
        repository, tag = name, ""
    return repository, tag + at + digest
