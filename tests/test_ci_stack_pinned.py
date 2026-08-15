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
asserts CI's `pip install` lines pin the same guarded packages to the same
versions, in *every* job that installs them, not only the python-tests job.

Plain pytest, no pipeline-runtime imports, all paths derived from
`__file__` per this repo's other guard tests (see test_layout.py,
test_no_duplicate_param_defaults.py).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
TILED_REQUIREMENTS = ROOT / "containers" / "tiled" / "requirements.txt"

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
    """The guarded set must not be silently narrowed by deleting a line from
    ci.yml — every guarded package name must appear at least once."""
    text = CI_WORKFLOW.read_text()
    found_names = {name for name, _ in find_guarded_pins(text)}
    missing = GUARDED_PACKAGES - found_names
    assert not missing, (
        f"Guarded package(s) {sorted(missing)} do not appear in any `pip "
        f"install` line in {CI_WORKFLOW.relative_to(ROOT)} at all. This "
        "test's guarded set must track packages CI actually installs — "
        "either CI stopped installing one of them (update GUARDED_PACKAGES) "
        "or a pip install line was deleted/reworded and needs restoring."
    )


def test_ci_guarded_packages_are_pinned_with_double_equals():
    """Every guarded-package token in any `pip install` line, in every job,
    must be pinned with `==` — not left to float to whatever the resolver
    picks that day."""
    text = CI_WORKFLOW.read_text()
    unpinned = [name for name, version in find_guarded_pins(text) if version is None]
    assert not unpinned, (
        f"{CI_WORKFLOW.relative_to(ROOT)} installs guarded package(s) "
        f"{sorted(set(unpinned))} without an `==` version pin. An unpinned "
        "imaging-library install lets CI resolve a different stack than "
        "the production containers, which is exactly the class of bug that "
        "produced a blank thumbnail (intensity p1/p50/p99.9 = 0.0/0.0/0.0) "
        "and 'RuntimeError: ORB found no features.' on unrelated tiled "
        "tests. Pin every occurrence with `==<version>`."
    )


def test_ci_guarded_pins_match_containers_tiled_requirements():
    """Every guarded package pinned in ci.yml must match the version pinned
    for that package in containers/tiled/requirements.txt — the production
    image that actually runs the tiled path."""
    ci_text = CI_WORKFLOW.read_text()
    tiled_pins = parse_tiled_requirements(TILED_REQUIREMENTS.read_text())

    assert tiled_pins.keys() >= GUARDED_PACKAGES, (
        f"{TILED_REQUIREMENTS.relative_to(ROOT)} does not pin all of "
        f"{sorted(GUARDED_PACKAGES)} — missing "
        f"{sorted(GUARDED_PACKAGES - tiled_pins.keys())}. This test's "
        "authority file must itself pin the full guarded set."
    )

    mismatches = []
    for name, version in find_guarded_pins(ci_text):
        if version is None:
            # Already reported by test_ci_guarded_packages_are_pinned_with_double_equals.
            continue
        expected = tiled_pins[name]
        if version != expected:
            mismatches.append(
                f"{name}=={version} in ci.yml but containers/tiled/"
                f"requirements.txt pins {name}=={expected}"
            )

    assert not mismatches, (
        "CI's guarded-package pins disagree with containers/tiled/"
        "requirements.txt (the production image that runs the tiled "
        "path):\n" + "\n".join(sorted(set(mismatches)))
    )
