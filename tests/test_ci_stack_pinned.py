#!/usr/bin/env python3
"""Guard: CI's imaging stack must be pinned to what the containers run.

`.github/workflows/ci.yml` used to `pip install` `tifffile numpy pandas
scikit-image scipy scikit-learn xmltodict` unpinned, then pin
`zarr==2.18.3 numcodecs==0.13.1` last (an "ordering hack" — the LAST pin in
a `pip install` invocation wins over anything dragged in transitively
earlier). That let CI resolve whatever the latest release of each guarded
package happened to be on a given day — e.g. a `tifffile` that requires
`zarr>=3.2`, silently downgraded back to `zarr==2.18.3` afterward, leaving an
inconsistent install. The result: CI read a blank thumbnail
(`intensity p1/p50/p99.9 = 0.0/0.0/0.0`) and `RuntimeError: ORB found no
features.` on tiled tests that have nothing to do with the change being
tested.

`containers/tiled/requirements.txt` is the authority — it's what the
production container that actually runs the tiled path installs. This test
asserts that `pip install` lines pin the same guarded packages to the same
versions, in *every* job that installs them, not only the python-tests job.

SCOPE: every workflow under `.github/workflows/`, discovered by glob — NOT a
hardcoded `ci.yml`. The hardcoded version of this guard passed green while
`.github/workflows/release.yml` ran the identical unpinned
`pip install pytest pytest-cov tifffile numpy pandas scikit-image` in its
release gate. Do not reintroduce a filename list here.

Plain pytest, no pipeline-runtime imports, all paths derived from
`__file__` per this repo's other guard tests (see test_layout.py,
test_no_duplicate_param_defaults.py).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
TILED_REQUIREMENTS = ROOT / "containers" / "tiled" / "requirements.txt"


def workflow_files() -> list[Path]:
    """Every workflow under `.github/workflows/`, DISCOVERED BY GLOB.

    This deliberately does not enumerate filenames. An earlier version of this
    guard hardcoded `ci.yml`, and `release.yml` sat next to it running
    `pip install pytest pytest-cov tifffile numpy pandas scikit-image` —
    unpinned, the exact defect this file exists to prevent — while every test
    here passed. Any new workflow is in scope the moment it is added.
    """
    files = sorted(WORKFLOWS_DIR.glob("*.yml")) + sorted(
        WORKFLOWS_DIR.glob("*.yaml")
    )
    assert files, f"no workflow files found under {WORKFLOWS_DIR.relative_to(ROOT)}"
    return files

# The exact guarded set per the brief — not "every imaging package", so a
# future package (e.g. scikit-learn) added to a pip install line doesn't
# silently need a containers/tiled pin it has no reason to have.
GUARDED_PACKAGES = frozenset(
    {"numpy", "scipy", "scikit-image", "tifffile", "zarr", "numcodecs"}
)

# `pip install <stuff>` lines are plain shell inside a YAML block scalar
# (`run: |`), so a raw line scan is sufficient — this is what the repo's
# other guard tests do (see test_layout.py's comment on the same approach).
_PIP_INSTALL_LINE_RE = re.compile(r"pip install\b(.*)$")
_NAME_VERSION_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9_.-]*)(?:==([A-Za-z0-9_.-]+))?$"
)


def _strip_comment(line: str) -> str:
    """Strip a trailing `# ...` comment. No `#` appears inside any quoted
    package spec this file needs to parse, so a plain split is sufficient."""
    return line.split("#", 1)[0]


def parse_pip_install_tokens(text: str) -> list[list[str]]:
    """Return one token list per `pip install` line found in `text`.

    Each line is comment-stripped, the text after `pip install` is
    tokenised on whitespace, and surrounding single/double quotes are
    dropped from each token (`'zarr==2.18.3'` -> `zarr==2.18.3`).
    """
    lines_tokens: list[list[str]] = []
    for raw_line in text.splitlines():
        line = _strip_comment(raw_line)
        m = _PIP_INSTALL_LINE_RE.search(line)
        if not m:
            continue
        rest = m.group(1)
        tokens = [tok.strip("'\"") for tok in rest.split()]
        lines_tokens.append(tokens)
    return lines_tokens


def find_guarded_pins(text: str) -> list[tuple[str, str | None]]:
    """Return (package, version_or_None) for every guarded-package token
    found across all `pip install` lines in `text`. `version` is None when
    the token names a guarded package with no `==` pin at all.
    """
    found: list[tuple[str, str | None]] = []
    for tokens in parse_pip_install_tokens(text):
        for tok in tokens:
            m = _NAME_VERSION_RE.match(tok)
            if not m:
                continue
            name, version = m.group(1), m.group(2)
            if name in GUARDED_PACKAGES:
                found.append((name, version))
    return found


def parse_tiled_requirements(text: str) -> dict[str, str]:
    """Parse `containers/tiled/requirements.txt` into {name: version}."""
    pins: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = _strip_comment(raw_line).strip()
        if not line:
            continue
        m = _NAME_VERSION_RE.match(line)
        if m and m.group(2):
            pins[m.group(1)] = m.group(2)
    return pins


def test_guarded_packages_appear_in_ci_workflow():
    """The guarded set must not be silently narrowed by deleting a line from a
    workflow — every guarded package name must appear at least once somewhere
    under `.github/workflows/`."""
    found_names: set[str] = set()
    for wf in workflow_files():
        found_names |= {name for name, _ in find_guarded_pins(wf.read_text())}
    missing = GUARDED_PACKAGES - found_names
    assert not missing, (
        f"Guarded package(s) {sorted(missing)} do not appear in any `pip "
        f"install` line in any workflow under "
        f"{WORKFLOWS_DIR.relative_to(ROOT)} at all. This test's guarded set "
        "must track packages CI actually installs — either CI stopped "
        "installing one of them (update GUARDED_PACKAGES) or a pip install "
        "line was deleted/reworded and needs restoring."
    )


def test_ci_guarded_packages_are_pinned_with_double_equals():
    """Every guarded-package token in any `pip install` line, in every job of
    every workflow, must be pinned with `==` — not left to float to whatever
    the resolver picks that day."""
    unpinned: list[str] = []
    for wf in workflow_files():
        for name, version in find_guarded_pins(wf.read_text()):
            if version is None:
                unpinned.append(f"{wf.relative_to(ROOT)}: {name}")
    assert not unpinned, (
        "workflow(s) install guarded package(s) without an `==` version "
        "pin:\n" + "\n".join(sorted(set(unpinned))) + "\n"
        "An unpinned imaging-library install lets CI resolve a different "
        "stack than the production containers, which is exactly the class of "
        "bug that produced a blank thumbnail (intensity p1/p50/p99.9 = "
        "0.0/0.0/0.0) and 'RuntimeError: ORB found no features.' on unrelated "
        "tiled tests. Pin every occurrence with `==<version>`."
    )


def test_ci_guarded_pins_match_containers_tiled_requirements():
    """Every guarded package pinned in any workflow must match the version
    pinned for that package in containers/tiled/requirements.txt — the
    production image that actually runs the tiled path."""
    tiled_pins = parse_tiled_requirements(TILED_REQUIREMENTS.read_text())

    assert tiled_pins.keys() >= GUARDED_PACKAGES, (
        f"{TILED_REQUIREMENTS.relative_to(ROOT)} does not pin all of "
        f"{sorted(GUARDED_PACKAGES)} — missing "
        f"{sorted(GUARDED_PACKAGES - tiled_pins.keys())}. This test's "
        "authority file must itself pin the full guarded set."
    )

    mismatches = []
    for wf in workflow_files():
        for name, version in find_guarded_pins(wf.read_text()):
            if version is None:
                # Already reported by
                # test_ci_guarded_packages_are_pinned_with_double_equals.
                continue
            expected = tiled_pins[name]
            if version != expected:
                mismatches.append(
                    f"{wf.relative_to(ROOT)}: {name}=={version} but "
                    f"containers/tiled/requirements.txt pins "
                    f"{name}=={expected}"
                )

    assert not mismatches, (
        "workflow guarded-package pins disagree with containers/tiled/"
        "requirements.txt (the production image that runs the tiled "
        "path):\n" + "\n".join(sorted(set(mismatches)))
    )
