"""Sphinx configuration of the NADOR documentation (same layout as the IFRAN documentation)."""  # noqa: INP001

import shutil
import sys
import tomllib
from datetime import date
from pathlib import Path

DOCS = Path(__file__).resolve().parent
ROOT = DOCS.parent
SRC = ROOT / "src"
META = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

# Make nador importable for autodoc
sys.path.insert(0, str(SRC))

# -- Project information -----------------------------------------------------
project = "NADOR: Network Analytics, Diagnostics, Observability, and Reporting"
release = version = META.get("version", "unknown")
author = ", ".join(a["name"] for a in META.get("authors", []) if "name" in a)
copyright = f"{date.today().year}, {author}"  # noqa: A001 (Sphinx setting name)

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
    "no-index": True,  # as IFRAN: the re-exports of nador/__init__.py would be documented twice
}
autodoc_member_order = "bysource"
# Optional runtime dependencies of a few modules, not needed to read the docstrings: imported as mocks
autodoc_mock_imports = ["kubernetes", "k8scontroller"]

source_suffix = {".rst": "restructuredtext", ".md": "markdown"}
myst_fence_as_directive = ["mermaid"]
myst_heading_anchors = 3
suppress_warnings = ["myst.xref_missing"]
exclude_patterns = ["_build", "reference/**/modules.rst"]

# -- HTML output -------------------------------------------------------------
html_title = project
html_short_title = "NADOR"
html_theme = "furo"


def generate_reference(app):  # noqa: ARG001 (Sphinx event handler signature)
    """API reference generated from the docstrings of the nador package (sphinx-apidoc) at each build."""
    from sphinx.ext.apidoc import main as apidoc  # noqa: PLC0415

    reference = DOCS / "reference"
    if reference.exists():
        shutil.rmtree(reference)
    apidoc(["--force", "--separate", "--module-first", "--tocfile", "index", "-o", str(reference), str(SRC / "nador")])


def setup(app):
    """Sphinx extension hook: regenerate the API reference before each build."""
    app.connect("builder-inited", generate_reference)
