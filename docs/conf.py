"""Sphinx configuration for the NADOR documentation (same layout as IFRAN's)."""

import shutil
import sys
import tomllib
from datetime import date
from pathlib import Path

DOCS = Path(__file__).resolve().parent
ROOT = DOCS.parent
PACKAGE = ROOT / "src" / "nador"

meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
sys.path.insert(0, str(ROOT / "src"))  # import nador from this checkout for autodoc

# -- Project information -----------------------------------------------------
project = "NADOR: Network Analytics, Diagnostics, Observability, and Reporting"
release = version = meta["version"]
author = ", ".join(entry["name"] for entry in meta.get("authors", []))
copyright = f"{date.today().year}, {author}"

# -- General configuration ---------------------------------------------------
root_doc = "index"
extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",  # Google / NumPy style docstrings
    "sphinx.ext.viewcode",
    "myst_parser",
    "sphinxcontrib.mermaid",
]
autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "show-inheritance": True,
    "no-index": True,
}
autodoc_member_order = "bysource"

source_suffix = {".rst": "restructuredtext", ".md": "markdown"}
myst_enable_extensions = ["colon_fence"]
myst_fence_as_directive = ["mermaid"]
myst_heading_anchors = 3
# The converted notebooks skip heading levels and contain shell lines (!pip ...) that the Python
# highlighter cannot color; both are cosmetic, and silencing them keeps `-W` useful for real errors.
suppress_warnings = ["myst.xref_missing", "myst.header", "misc.highlighting_failure"]
exclude_patterns = ["_build"]

# -- HTML output -------------------------------------------------------------
html_title = project
html_short_title = "NADOR"
html_theme = "furo"


def generate_reference(app) -> None:
    """Write the API reference with sphinx-apidoc, leaving out the files mounted into containers."""
    from sphinx.ext.apidoc import main as apidoc

    reference = DOCS / "reference"
    shutil.rmtree(reference, ignore_errors=True)
    apidoc(
        [
            "--force",
            "--separate",
            "--module-first",
            "--implicit-namespaces",
            "--tocfile",
            "index",
            "-o",
            str(reference / "nador"),
            str(PACKAGE),
            str(PACKAGE / "assets"),  # charts, roles and their scripts: not part of the Python API
        ]
    )
    (reference / "index.rst").write_text(
        "API Reference\n=============\n\n"
        "Generated from the docstrings of the ``nador`` package.\n\n"
        ".. toctree::\n   :maxdepth: 3\n\n   nador/index\n",
        encoding="utf-8",
    )


def setup(app) -> None:
    app.connect("builder-inited", generate_reference)
