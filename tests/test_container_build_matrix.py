"""containers/images.json is the ONE list of images to build. Keep it honest.

This guards the failure that shipped `:tiled` unpublished: a new build context
appears under containers/, nothing adds it to the build matrix, CI stays green
because nothing references it, and the first run that pulls the tag fails on the
cluster with "manifest unknown" -- long after the change looked done.

The list is also read by the workflow's `only` filter, so a name here that has no
directory produces a dispatch option that always fails.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
IMAGES_JSON = REPO_ROOT / "containers" / "images.json"
CONTAINERS = REPO_ROOT / "containers"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-images.yml"

# VALIS is deliberately not built here: the pipeline pulls the maintained upstream
# image cdgatenbee/valis-wsi:1.0.0 rather than re-hosting its from-source libvips
# build. Recorded as an exclusion so "missing" reads as "decided", not "forgotten".
NOT_BUILT_HERE: dict[str, str] = {}


def _entries() -> list[dict]:
    return json.loads(IMAGES_JSON.read_text())


def _build_context_dirs() -> set[str]:
    return {d.name for d in CONTAINERS.iterdir()
            if d.is_dir() and (d / "Dockerfile").exists()}


def test_every_build_context_is_in_the_matrix():
    listed = {e["dir"] for e in _entries()}
    missing = sorted(_build_context_dirs() - listed - set(NOT_BUILT_HERE))
    assert not missing, (
        f"containers/ has build context(s) absent from images.json: {missing}. "
        "An unlisted image is never built or pushed -- the run stays green and the "
        "tag simply does not exist on Docker Hub. Add it, or add it to "
        "NOT_BUILT_HERE with the reason.")


def test_every_matrix_entry_has_a_dockerfile():
    missing = sorted(e["dir"] for e in _entries()
                     if not (CONTAINERS / e["dir"] / "Dockerfile").exists())
    assert not missing, (
        f"images.json names dirs with no Dockerfile: {missing}. The matrix job "
        "would fail, and the `only` filter would offer a name that cannot build.")


def test_tags_are_unique_and_descriptive():
    """One Docker Hub repo, one descriptive tag per image. A duplicate tag means
    two images silently overwrite each other on push."""
    tags = [e["tag"] for e in _entries()]
    dupes = sorted({t for t in tags if tags.count(t) > 1})
    assert not dupes, f"duplicate tags in images.json: {dupes}"
    assert "latest" not in tags, "never :latest -- see containers/README.md"


def test_workflow_reads_the_json_rather_than_repeating_it():
    """The matrix must come from images.json. A hand-written `include:` list back
    in the workflow is the drift this file exists to prevent."""
    wf = WORKFLOW.read_text()
    assert "fromJson(needs.setup.outputs.matrix)" in wf, (
        "build-images.yml no longer builds its matrix from containers/images.json")
    assert "containers/images.json" in wf, (
        "build-images.yml does not read containers/images.json")
    # No inline per-image list left behind.
    assert not re.search(r"^\s*- \{ dir: \w+", wf, re.M), (
        "build-images.yml still carries an inline image list; images.json is the "
        "single source and a second copy will drift")


def test_modules_pull_only_tags_the_matrix_publishes():
    """Every bolt3x/attend_image_analysis:<tag> a module pulls must be a tag this
    workflow actually builds -- otherwise the pipeline references an image nothing
    publishes, which is exactly how :tiled shipped broken."""
    published = {e["tag"] for e in _entries()}
    # Tags supplied by a param default rather than a literal are resolved from
    # nextflow.config, which is where the pipeline's own default lives.
    cfg = (REPO_ROOT / "nextflow.config").read_text()
    referenced: set[str] = set()
    for nf in (REPO_ROOT / "modules" / "local").glob("*.nf"):
        for m in re.finditer(r"attend_image_analysis:\$\{params\.(\w+)\}|"
                             r"attend_image_analysis:([\w.\-]+)", nf.read_text()):
            param, literal = m.group(1), m.group(2)
            if literal:
                referenced.add(literal)
            else:
                dm = re.search(rf"^\s*{param}\s*=\s*'([^']+)'", cfg, re.M)
                if dm:
                    referenced.add(dm.group(1))
    unknown = sorted(referenced - published)
    assert not unknown, (
        f"modules pull tag(s) this workflow never builds: {unknown}. Either add a "
        f"build context + images.json entry, or fix the tag. Published: "
        f"{sorted(published)}")
