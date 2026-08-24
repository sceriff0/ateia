"""Container image names are a published interface, and this file is where their shape is fixed.

Every first-party image is ``bolt3x/mirage-<component>:<version>`` -- one Docker Hub repository
per image, the layout nf-core, biocontainers and the quay.io projects use. The previous layout
put ten unrelated images in a single repository (``bolt3x/attend_image_analysis``) and used the
*tag* to say which image it was. That has three consequences worth naming, because each one is
a thing that actually went wrong:

  * The tag namespace ended up doing two jobs at once. The live repository holds component names
    (``preprocess``, ``tiled``) next to genuine versions (``v2.3``, ``v2.4``) next to a decade of
    dead images (``pixie``, ``fastmorph``, ``stardist_gpu``). Nothing in that list can be ordered
    or compared, because Docker Hub treats one repository as one artifact lineage.
  * There were no immutable artifacts. ``build-images.yml`` asked the operator for a version on
    every ``workflow_dispatch``, resolved it into ``setup.outputs.version`` -- and then never put
    it in the tag, publishing to the same mutable name regardless. There was no image to roll
    back TO. ``test_the_build_workflow_applies_the_version_to_the_tag`` is the guard for that
    specific bug.
  * Names drifted from what they name. ``instant_seg`` and its build directory ``istantseg``
    misspelled InstanSeg two different ways; ``convert_bioformats_2`` carried a meaningless
    ``_2``; ``segmentation_gpu`` and ``quantification_gpu`` carried a ``_gpu`` suffix that
    ``cellsam`` and ``instant_seg`` -- equally GPU images -- did not; and ``debug_diffeo`` said
    "debug" while running GENERATE_REGISTRATION_QC in production.

The version in the tag is tied to ``manifest.version`` so an image set and the pipeline that
pulls it cannot drift apart silently.
"""

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

NAMESPACE = "bolt3x"
PREFIX = "mirage-"

# Third-party images the pipeline pulls as-is. VALIS is upstream-maintained and deliberately not
# re-hosted (its from-source libvips build is heavy); ubuntu is a plain base for a trivial task.
# basicpy-docker-mcmicro is the vendored nf-core BASICPY module's own container (mcmicro-maintained);
# BASICPY is a vendored module (modules/nf-core/basicpy/), not a bolt3x/mirage-<component> build, so
# re-hosting it would mean maintaining a fork of the vendored image too.
# labsyspharm/ashlar is the upstream ASHLAR image, used by ASHLAR_SOLVE alone. It is NOT
# re-hosted as bolt3x/mirage-ashlar deliberately: .github/workflows/build-images.yml publishes
# only on release / workflow_dispatch / workflow_call, never on a push to main, so a new
# containers/ashlar/ build context would go green and unpublished and every ashlar arm would
# fail pulling a tag that does not exist. Pulling upstream also keeps the comparison honest --
# the baseline is the image the ASHLAR authors ship, not a mirage rebuild of it. Note it is
# amd64-only: fine on the cluster, qemu-slow on arm64.
EXTERNAL_ALLOWED = {
    "cdgatenbee/valis-wsi:1.0.0",
    "ubuntu:22.04",
    "docker.io/labsyspharm/basicpy-docker-mcmicro:1.2.0-patch5",
    "labsyspharm/ashlar:1.20.0",
}

# The retired single-repository name. Nothing may reference it again.
LEGACY_REPO = "attend_image_analysis"

_IMAGE_REF = re.compile(r"""['"]([A-Za-z0-9][A-Za-z0-9._/-]*:[A-Za-z0-9][A-Za-z0-9._-]*)['"]""")
_FIRST_PARTY = re.compile(rf"^{NAMESPACE}/{PREFIX}([a-z0-9]+(?:-[a-z0-9]+)*):(\d+\.\d+\.\d+)$")


def _manifest_version():
    text = (REPO / "nextflow.config").read_text()
    block = re.search(r"manifest\s*\{(.*?)\n\}", text, re.S).group(1)
    return re.search(r"version\s*=\s*'([^']+)'", block).group(1)


def _searched_files():
    return (
        sorted((REPO / "modules" / "local").glob("*.nf"))
        + sorted((REPO / "modules" / "nf-core").rglob("*.nf"))
        + sorted((REPO / "lib").glob("*.groovy"))
        + sorted((REPO / "conf").glob("*.config"))
    )


def _image_refs():
    """(file, ref) for every quoted image reference in the pipeline's own sources."""
    out = []
    for path in _searched_files():
        for line in path.read_text().splitlines():
            if "container" not in line and "Image" not in line:
                continue
            for ref in _IMAGE_REF.findall(line):
                if "/" in ref or ref in EXTERNAL_ALLOWED:
                    out.append((path.relative_to(REPO).as_posix(), ref))
    return out


def _container_dirs():
    root = REPO / "containers"
    return sorted(p.name for p in root.iterdir() if (p / "Dockerfile").is_file())


def test_no_source_references_the_legacy_single_repository():
    """The one-repo-many-tags layout is retired; a stray reference would pull a stale image.

    Scans ``containers/`` itself (every file, recursively) in addition to the pipeline sources
    ``_searched_files()`` already covers: a Dockerfile's own build/push comments can reference
    the legacy repository without the string ever appearing in a `container` directive that
    ``_searched_files()``'s modules/local + lib + conf scan would catch -- exactly the bug this
    guard missed in containers/convert/Dockerfile, which still named
    ``bolt3x/attend_image_analysis:bioformats_v1`` in its build/push comments after the rename.
    """
    offenders = []
    containers_files = sorted(p for p in (REPO / "containers").rglob("*") if p.is_file())
    for path in _searched_files() + containers_files + [
        REPO / "CLAUDE.md", REPO / ".github/workflows/build-images.yml"
    ]:
        if not path.is_file():
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if LEGACY_REPO in line:
                offenders.append(f"{path.relative_to(REPO)}:{i}")
    assert not offenders, (
        f"{LEGACY_REPO} is the retired single repository but is still referenced at {offenders}."
    )


def test_every_image_reference_is_either_first_party_or_an_allowed_external():
    bad = [
        (f, r) for f, r in _image_refs()
        if r not in EXTERNAL_ALLOWED and not _FIRST_PARTY.match(r)
    ]
    assert not bad, (
        f"image references that are neither {NAMESPACE}/{PREFIX}<component>:<x.y.z> nor an "
        f"allowed external image: {bad}. Allowed externals are {sorted(EXTERNAL_ALLOWED)}."
    )


def test_first_party_images_are_pinned_to_the_manifest_version():
    """An image set and the pipeline that pulls it must not drift apart silently."""
    want = _manifest_version()
    wrong = [
        (f, r) for f, r in _image_refs()
        if (m := _FIRST_PARTY.match(r)) and m.group(2) != want
    ]
    assert not wrong, (
        f"these images are not pinned to manifest.version ({want}): {wrong}"
    )


def test_every_referenced_component_has_a_build_context():
    missing = []
    dirs = set(_container_dirs())
    for f, r in _image_refs():
        m = _FIRST_PARTY.match(r)
        if m and m.group(1) not in dirs:
            missing.append((f, r, m.group(1)))
    assert not missing, (
        f"referenced images with no containers/<component>/Dockerfile to build them: {missing}. "
        f"Existing build contexts: {sorted(dirs)}"
    )


def test_the_build_workflow_covers_every_build_context_exactly_once():
    """The matrix must cover every build context on disk, exactly once.

    THE MATRIX MOVED. This test used to parse an inline `{ dir: convert }` list out of
    build-images.yml. That list is gone: `containers/images.json` is now the single source
    the workflow's matrix and its `only` filter both read, and
    tests/test_container_build_matrix.py FAILS if an inline copy reappears in the workflow
    -- because a second copy drifts, and a context missing from one of them is never built
    while the run stays green. So the two guards had directly contradictory expectations
    when the streaming line met the benchmarking line; this one now reads the same single
    source, which serves both intents rather than picking a winner.
    """
    entries = json.loads((REPO / "containers/images.json").read_text())
    listed = [e["dir"] for e in entries]
    assert sorted(listed) == sorted(set(listed)), f"duplicate matrix entries: {listed}"
    assert set(listed) == set(_container_dirs()), (
        f"build matrix {sorted(set(listed))} does not match the build contexts on disk "
        f"{_container_dirs()}. An unlisted context is never built or published."
    )
    workflow = (REPO / ".github/workflows/build-images.yml").read_text()
    assert "fromJson(needs.setup.outputs.matrix)" in workflow, (
        "build-images.yml must take its matrix from the setup job's images.json-derived "
        "output; an inline list is a second copy that drifts"
    )


def test_the_build_workflow_applies_the_version_to_the_tag():
    """The specific bug: a required `version` input that never reached the published tag.

    Without this, every publish overwrites one mutable name and there is no prior image to roll
    back to -- while the workflow's UI still asks for a version, implying otherwise.
    """
    text = (REPO / ".github/workflows/build-images.yml").read_text()
    tags_line = re.search(r"^\s*tags:\s*(.+)$", text, re.M)
    assert tags_line, "build-images.yml has no tags: line"
    assert "version" in tags_line.group(1), (
        f"the published tag {tags_line.group(1).strip()!r} does not include the resolved "
        f"version, so every push overwrites the same mutable tag and nothing is rollback-able."
    )


@pytest.mark.parametrize("name", _container_dirs())
def test_build_context_directories_use_the_component_name(name):
    """The directory, the image and the pipeline reference must all say the same word."""
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name), (
        f"containers/{name} is not a lowercase hyphenated component name, so it cannot map "
        f"cleanly onto a Docker Hub repository."
    )


def test_the_build_workflow_strips_a_leading_v_from_the_version():
    """GitHub release tags are v1.0.0; manifest.version -- and so the image reference -- is 1.0.0.

    Without normalisation the release path publishes mirage-<component>:v1.0.0 while every
    process pulls mirage-<component>:1.0.0, so a release would build ten images that nothing
    can pull and fail only once a run reaches the first process.
    """
    text = (REPO / ".github/workflows/build-images.yml").read_text()
    assert "${VERSION#v}" in text, (
        "build-images.yml does not strip a leading 'v' from the resolved version, so a GitHub "
        "release tagged v1.0.0 publishes images the pipeline cannot pull."
    )


def test_the_build_workflow_preflights_the_registry_credentials():
    """Missing credentials must fail once, in `setup`, with a message that says what is missing.

    Without a preflight the run fans out to one job per image and each dies separately on
    docker/login-action's "Username and password required", which names neither the secret nor
    the fix. That is what happened the first time the publish path was ever exercised.

    The publish path had been unexercised for months while the workflow reported success: the
    login step is guarded by `if: needs.setup.outputs.push == 'true'`, and every prior run was a
    push-to-main build-only event, so the credentials were never reached. A green Build Images
    run therefore said nothing about whether publishing worked.
    """
    text = (REPO / ".github/workflows/build-images.yml").read_text()
    assert "DOCKERHUB_USERNAME" in text and "DOCKERHUB_TOKEN" in text
    preflight = re.search(r"name: Preflight.*?(?=\n      - name:|\n  [a-z])", text, re.S)
    assert preflight, (
        "build-images.yml has no preflight step, so a publish with missing secrets fans out "
        "into one opaque failure per image instead of failing once with a readable reason."
    )
    body = preflight.group(0)
    assert "secrets.DOCKERHUB_TOKEN" in body and "exit 1" in body, (
        "the preflight step does not actually check the token and fail: " + body[:300]
    )
