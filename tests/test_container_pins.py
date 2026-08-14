#!/usr/bin/env python3
"""Guard the container -> image -> tifffile-pin mapping (Task 11b).

Two things rot silently here, and both bit before this guard existed:

1. `container:` directives in `modules/local/*.nf` (plus the two dynamic
   dispatch tables, `lib/SegBackends.groovy` and `lib/WarpBackends.groovy`)
   name a `bolt3x/attend_image_analysis:<tag>` image. Nothing checked that
   `<tag>` actually corresponds to one of the ten build contexts under
   `containers/`, or to the one deliberately-external allowlisted image
   (`cdgatenbee/valis-wsi:1.0.0`) plus the general-purpose `ubuntu:22.04`
   used by AGGREGATE_SIZE_LOGS (no imaging libraries, no build context of
   its own). A typo'd tag builds and runs fine locally (Docker resolves the
   tag against whatever is on the machine / registry) and only fails on a
   clean pull -- exactly the kind of thing that "goes green and unpublished"
   per CI's image-build workflow not pushing on `main`.

2. `tifffile` -- the one imaging library every one of these ten images
   installs, directly or transitively -- was pinned to at least five
   different versions, three images didn't pin it at all, and one more
   pinned it only implicitly (as a side effect of `aicsimageio==4.14.0`'s
   own upper bound). See `docs/containers.md` for the full per-image
   evidence and reasoning; this file only asserts the outcome holds.

Both checks are static (regex over the Dockerfile / requirements.txt /
.groovy text) because the hard constraint on this task is that no image can
be built or pulled here (arm64, Docker hangs on pull). See CLAUDE.md's
"Verification reality" and the task-11b brief.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULES_DIR = ROOT / "modules" / "local"
CONTAINERS_DIR = ROOT / "containers"
LIB_DIR = ROOT / "lib"

# --------------------------------------------------------------------------
# The registry: containers/<dir> -> the bolt3x/attend_image_analysis:<tag> it
# builds. This is the one place that mapping is spelled out for a test (it's
# also documented in prose in containers/README.md and docs/containers.md --
# this table is the machine-checked source of truth the docs must agree
# with, not a competing one).
# --------------------------------------------------------------------------
DIR_TO_TAG = {
    "bioformats": "convert_bioformats_2",
    "cellsam": "cellsam",
    "debug_diffeo": "debug_diffeo",
    "istantseg": "instant_seg",  # historical typo'd dir name; see containers/README.md
    "merge": "merge",
    "preprocess": "preprocess",
    "quantification": "quantification_gpu",
    "segmentation": "segmentation_gpu",
    "spatialdata": "spatialdata",
    "tiled": "tiled",
}

# Images referenced by `container:` directives that do NOT come from
# containers/ -- either a deliberately-unvendored upstream image, or a
# generic base image with no imaging libraries and no build context of its
# own. Anything else outside DIR_TO_TAG's values is an error.
EXTERNAL_ALLOWLIST = {
    "cdgatenbee/valis-wsi:1.0.0",  # upstream VALIS; see containers/README.md "not vendored"
    "ubuntu:22.04",  # AGGREGATE_SIZE_LOGS -- coreutils only, no imaging libs
}

IMAGE_PREFIX = "bolt3x/attend_image_analysis:"

# A `container 'IMAGE'` / `container "IMAGE"` directive line (static string
# form). Anchored on the `container` keyword so we don't match the word
# appearing elsewhere (comments, etc.) -- same anchoring style as
# tests/test_resource_label_coverage.py's LABEL_LINE_RE.
CONTAINER_LINE_RE = re.compile(r"^\s*container\s+(['\"])([^'\"]+)\1\s*$", re.MULTILINE)

# The dynamic form: `container { ClassName.container(...) }`, used by
# SEGMENT (lib/SegBackends.groovy) and WARP_SEG_QC (lib/WarpBackends.groovy).
DYNAMIC_CONTAINER_RE = re.compile(r"container\s*\{\s*(\w+)\.container\(")

# `container : 'IMAGE',` entries inside a *Backends.groovy BACKENDS map.
BACKEND_CONTAINER_ENTRY_RE = re.compile(r"container\s*:\s*'([^']+)'")


def _static_container_images() -> dict[str, list[str]]:
    """{image -> [modules/local/*.nf files that reference it statically]}."""
    found: dict[str, list[str]] = {}
    for path in sorted(MODULES_DIR.glob("*.nf")):
        text = path.read_text()
        for _quote, image in CONTAINER_LINE_RE.findall(text):
            found.setdefault(image, []).append(str(path.relative_to(ROOT)))
    return found


def _dynamic_container_classes() -> dict[str, list[str]]:
    """{ClassName -> [modules/local/*.nf files dispatching to it]}."""
    found: dict[str, list[str]] = {}
    for path in sorted(MODULES_DIR.glob("*.nf")):
        text = path.read_text()
        for class_name in DYNAMIC_CONTAINER_RE.findall(text):
            found.setdefault(class_name, []).append(str(path.relative_to(ROOT)))
    return found


def _images_from_backend_class(class_name: str) -> list[str]:
    lib_file = LIB_DIR / f"{class_name}.groovy"
    assert lib_file.is_file(), (
        f"modules/local dispatches container selection to '{class_name}' "
        f"but lib/{class_name}.groovy does not exist."
    )
    return BACKEND_CONTAINER_ENTRY_RE.findall(lib_file.read_text())


def all_referenced_images() -> dict[str, list[str]]:
    """{image -> [referencing files]} across static directives and both
    dynamic dispatch tables (lib/SegBackends.groovy, lib/WarpBackends.groovy)."""
    referenced = _static_container_images()
    for class_name, files in _dynamic_container_classes().items():
        for image in _images_from_backend_class(class_name):
            referenced.setdefault(image, []).extend(
                f"{f} (via lib/{class_name}.groovy)" for f in files
            )
    return referenced


def test_containers_dir_has_exactly_the_ten_registered_images():
    """Sanity check on the registry itself: containers/ must define exactly
    the ten build-context directories DIR_TO_TAG names, no more, no fewer.
    This is what makes the mapping test below actually exercise all ten
    images instead of silently scoping past new or renamed ones."""
    actual_dirs = {p.name for p in CONTAINERS_DIR.iterdir() if p.is_dir()}
    assert actual_dirs == set(DIR_TO_TAG), (
        "containers/ build-context directories no longer match this test's "
        f"registry.\n  containers/ has: {sorted(actual_dirs)}\n"
        f"  DIR_TO_TAG has:   {sorted(DIR_TO_TAG)}\n"
        "Update DIR_TO_TAG (and docs/containers.md) for the new/removed image."
    )
    for name in DIR_TO_TAG:
        dockerfile = CONTAINERS_DIR / name / "Dockerfile"
        assert dockerfile.is_file(), f"containers/{name}/Dockerfile is missing"


def test_every_container_directive_names_a_known_image():
    """Every `container:` a modules/local/*.nf process asks for must be
    either one of the ten containers/ build contexts (by its published tag)
    or an explicitly allowlisted external image. A name that matches
    neither is a typo or a stale reference that will fail on a clean pull."""
    referenced = all_referenced_images()
    known_tags = set(DIR_TO_TAG.values())

    offending = []
    for image, files in sorted(referenced.items()):
        if image in EXTERNAL_ALLOWLIST:
            continue
        if image.startswith(IMAGE_PREFIX):
            tag = image[len(IMAGE_PREFIX) :]
            if tag in known_tags:
                continue
            offending.append(
                f"{image!r} (tag {tag!r} matches no containers/ build context; "
                f"referenced by {', '.join(files)})"
            )
            continue
        offending.append(
            f"{image!r} is neither a bolt3x/attend_image_analysis tag nor in "
            f"EXTERNAL_ALLOWLIST (referenced by {', '.join(files)})"
        )

    assert not offending, (
        f"{len(offending)} container directive(s) name an image that containers/ "
        "does not define and that is not explicitly allowlisted:\n"
        + "\n".join(offending)
    )


# --------------------------------------------------------------------------
# tifffile pin coverage.
# --------------------------------------------------------------------------

# `tifffile` followed by a version comparator and a version -- i.e. an
# *explicit* pin/floor/ceiling, not a bare `tifffile` line that floats to
# whatever is newest at build time.
TIFFFILE_PIN_RE = re.compile(r"\btifffile\s*(?:==|>=|<=|~=)\s*[0-9][0-9A-Za-z.]*")

# Whether a Dockerfile actually wires its requirements.txt into the build
# (COPY + `pip install ... -r requirements.txt`). A requirements.txt that
# exists on disk but is never COPYed/installed is dead text -- checking it
# would validate a pin that has no effect on the image. (containers/segmentation
# is exactly this case: see docs/containers.md.)
REQUIREMENTS_WIRED_RE = re.compile(
    r"COPY\s+requirements\.txt.*?RUN\s+pip install.*?-r\s+\S*requirements\.txt",
    re.DOTALL,
)


def _requirements_txt_dirs() -> list[str]:
    return sorted(
        p.parent.name
        for p in CONTAINERS_DIR.glob("*/requirements.txt")
    )


def test_every_requirements_txt_pins_tifffile_explicitly():
    """Every `containers/*/requirements.txt` that exists must pin tifffile
    with an explicit version comparator (`==` or a documented `>=` floor for
    the one deliberately-loose image, spatialdata). A bare `tifffile` line
    means the resolved version drifts on every rebuild with no code change."""
    offending = []
    for path in sorted(CONTAINERS_DIR.glob("*/requirements.txt")):
        text = path.read_text()
        if not TIFFFILE_PIN_RE.search(text):
            offending.append(str(path.relative_to(ROOT)))

    assert not offending, (
        f"{len(offending)} requirements.txt file(s) do not pin tifffile "
        f"explicitly (no 'tifffile==' / 'tifffile>=' line found):\n"
        + "\n".join(offending)
    )


def test_every_image_pins_tifffile_explicitly():
    """The real per-image guard: for each of the ten containers/ images,
    find wherever its tifffile pin actually lives at build time --
    requirements.txt if the Dockerfile wires it in, otherwise the
    Dockerfile's own inline `pip install` -- and assert it is an explicit
    version, not a bare `tifffile` (floats) or no mention at all (transitive,
    resolved by whatever else happens to be installed).

    This is the guard that would have caught (and, pre-fix, DID catch) the
    three images with no pin at all (merge, quantification, istantseg) plus
    the fourth with only an implicit floor via aicsimageio==4.14.0
    (segmentation) -- four of the ten images, not the three the container
    audit's headline number counts, because that number only counted images
    with zero tifffile mention anywhere, not segmentation's implicit case.
    """
    offending = []
    for name in sorted(DIR_TO_TAG):
        dockerfile_path = CONTAINERS_DIR / name / "Dockerfile"
        dockerfile_text = dockerfile_path.read_text()
        requirements_path = CONTAINERS_DIR / name / "requirements.txt"

        if requirements_path.is_file() and REQUIREMENTS_WIRED_RE.search(dockerfile_text):
            live_text = requirements_path.read_text()
            live_source = requirements_path.relative_to(ROOT)
        else:
            live_text = dockerfile_text
            live_source = dockerfile_path.relative_to(ROOT)

        if not TIFFFILE_PIN_RE.search(live_text):
            offending.append(f"{name} (containers/{name}): no explicit pin in {live_source}")

    assert not offending, (
        f"{len(offending)} image(s) do not pin tifffile explicitly at their "
        "live build-time source:\n" + "\n".join(offending)
    )
