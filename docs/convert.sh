#!/bin/sh
# Convert the quickstart and monitoring example notebooks into Markdown pages for Sphinx.
# Run from the repository root, before sphinx-build (Read the Docs and CI do).
set -e

find examples/quickstart examples/monitoring -type f -name "*.ipynb" | sort | while read -r file; do
    rel_dir=$(dirname "${file#examples/}")
    out_dir="docs/examples/markdown/$rel_dir"
    mkdir -p "$out_dir"
    jupyter nbconvert --to markdown "$file" --output-dir "$out_dir"
    # Figures the notebook shows (nbconvert does not copy files it only links to).
    if [ -d "$(dirname "$file")/figures" ]; then
        cp -r "$(dirname "$file")/figures" "$out_dir/"
    fi
done
