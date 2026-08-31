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

Two guarded packages have a different authority *within the same image*:
`torch` and `kornia` are pinned in `containers/tiled/Dockerfile`, not in its
requirements.txt, because torch needs a CPU `--index-url` and kornia must be
installed after it — neither is expressible in a flat requirements file. See
`NON_TILED_AUTHORITY` and
`test_ci_torch_and_kornia_pins_match_the_tiled_dockerfile`.

SCOPE: every workflow under `.github/workflows/`, discovered by glob — NOT a
hardcoded `ci.yml`. The hardcoded version of this guard passed green while
`.github/workflows/release.yml` ran the identical unpinned
`pip install pytest pytest-cov tifffile numpy pandas scikit-image` in its
release gate. Do not reintroduce a filename list here.

But glob scope is not the same as per-file scope, and the *presence* check
below (`test_guarded_packages_appear_in_ci_workflow`) asks only whether a
package appears SOMEWHERE across that glob — a union, which one workflow can
satisfy on another's behalf. That masked a measured break: deleting both torch
and kornia from `ci.yml` alone left `release.yml`'s copies standing and this
whole file passed. `test_every_pytest_workflow_installs_the_importorskip_
guarded_packages` closes it PER FILE, for the packages whose absence produces a
silent skip rather than a loud ImportError. The workflow set it iterates is
still discovered — by looking for a `pytest ... tests/` invocation — never
named.

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
TILED_DOCKERFILE = ROOT / "containers" / "tiled" / "Dockerfile"


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
    {
        "numpy", "scipy", "scikit-image", "tifffile", "zarr", "numcodecs", "dask",
        # Added 2026-08-26, both after CI failed on their ABSENCE while this file
        # stayed green -- the guarded set excluded them, so a workflow that
        # installed neither satisfied every check here.
        #
        # imagecodecs: the zstd/LZW codec backend tifffile calls through. Missing
        # it is not an import error; it surfaces only on a WRITE, as
        # `ModuleNotFoundError: No module named 'compression'` raised from inside
        # tifffile. 34 tests in tests/test_merge_pyramid_streaming.py failed that
        # way -- every case writing with compression='zstd'/'zlib' -- and they
        # passed on any machine that happened to carry imagecodecs.
        "imagecodecs",
        # matplotlib: a hard dependency of the CSE evaluation path (see
        # NON_TILED_AUTHORITY below for why its version authority is not the
        # tiled image). bin/seg_quality_eval.py exits 1 without it.
        "matplotlib",
        # torch/kornia: STARE's DISK+LightGlue COARSE front-end
        # (bin/utils/coarse_align.py::_frontend_disk_lightglue). Guarded because
        # tests/test_coarse_frontend.py's DISK cases `importorskip` them -- an
        # unpinned or absent install turns those cases into silent skips, which is
        # exactly how an unimplemented front-end shipped once already. Their
        # authority is the tiled DOCKERFILE, not its requirements.txt; see
        # NON_TILED_AUTHORITY.
        "torch",
        "kornia",
    }
)

# Guarded packages whose version authority is NOT containers/tiled/requirements.txt.
# Each has its own check below; membership here only exempts it from the
# tiled-authority comparison, never from the "must be ==-pinned" rule.
#
# The tiled image is the right authority for the packages the tiled PATH runs on.
# For a package that path never imports, pinning it in containers/tiled/
# requirements.txt to satisfy this file would assert a dependency the image does
# not have -- so the package gets an authority that reflects where it actually
# runs instead.
NON_TILED_AUTHORITY = frozenset({
    "dask",        # authority: the three Dockerfiles that pin it, harmonised.
    "matplotlib",  # authority: containers/segeval, the image that runs CSE.
    # torch/kornia ARE tiled-image packages, but they are pinned in
    # containers/tiled/Dockerfile rather than its requirements.txt: torch needs
    # `--index-url https://download.pytorch.org/whl/cpu` (a requirements-file index
    # directive would repoint EVERY package) and kornia must be installed AFTER torch
    # or it drags the ~2.5 GB CUDA wheel from PyPI. Neither constraint is expressible
    # in a flat requirements file, so the Dockerfile is their authority.
    "torch",
    "kornia",
})

# `pip install <stuff>` lines are plain shell inside a YAML block scalar
# (`run: |`), so a raw line scan is sufficient — this is what the repo's
# other guard tests do (see test_layout.py's comment on the same approach).
_PIP_INSTALL_LINE_RE = re.compile(r"pip install\b(.*)$")
# The optional `[extras]` group (non-capturing) handles pip's extras syntax --
# `dask[array]==2026.7.1` -- which none of the OTHER guarded packages use, but dask
# always does in every pip install line in this repo. Without it the token fails the
# full-string match entirely (the `[`/`]` are outside the character class) and is
# silently dropped, not merely mis-parsed.
_NAME_VERSION_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[A-Za-z0-9_,.-]+\])?(?:==([A-Za-z0-9_.-]+))?$"
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


# The packages whose absence turns a test into a SILENT SKIP rather than a failure.
# tests/test_coarse_frontend.py's DISK+LightGlue cases call
# pytest.importorskip("torch")/importorskip("kornia"); every other guarded package is
# imported unconditionally somewhere, so its absence fails the job loudly.
IMPORTORSKIP_GUARDED = frozenset({"torch", "kornia"})

# `pytest ... tests/` -- the DIRECTORY form, i.e. a job that runs the whole suite.
# Discovered, never a filename list (see workflow_files()'s docstring for why).
_RUNS_THE_PYTEST_SUITE_RE = re.compile(r"pytest\b[^\n]*\btests/(?=\s|$)")


def workflows_running_the_pytest_suite() -> list[Path]:
    """Every workflow whose `run:` block invokes pytest over `tests/`."""
    hits = [wf for wf in workflow_files() if _RUNS_THE_PYTEST_SUITE_RE.search(wf.read_text())]
    assert hits, (
        f"no workflow under {WORKFLOWS_DIR.relative_to(ROOT)} runs `pytest ... tests/`; "
        "the per-file check below would pass over an empty set"
    )
    return hits


def test_every_pytest_workflow_installs_the_importorskip_guarded_packages():
    """PER-FILE, because a union presence check is a mask for these two.

    `test_guarded_packages_appear_in_ci_workflow` above asks only whether a guarded
    package appears SOMEWHERE under `.github/workflows/`. Measured blind spot: deleting
    both `pip install torch==2.3.1 --index-url ...` and `pip install kornia==0.7.3` from
    `.github/workflows/ci.yml` -- the workflow whose `python-tests` job is CI's blocking
    gate -- left `release.yml`'s copies standing, and this whole file passed. In that
    state ci.yml still runs the suite, tests/test_coarse_frontend.py's DISK cases
    importorskip away, and the gate is green having exercised no front-end at all.

    That mask matters for exactly the importorskip-guarded packages: for every other
    guarded package, a workflow that fails to install it fails loudly on an ImportError
    and needs no static guard to notice. See tests/test_disk_test_actually_runs.py for
    the companion check that the DISK case is not merely installable but actually runs.
    """
    missing: list[str] = []
    for wf in workflows_running_the_pytest_suite():
        installed = {name for name, _ in find_guarded_pins(wf.read_text())}
        for pkg in sorted(IMPORTORSKIP_GUARDED - installed):
            missing.append(f"{wf.relative_to(ROOT)}: no `pip install` of {pkg}")
    assert not missing, (
        "workflow(s) run the pytest suite without installing a package that the suite "
        "guards with pytest.importorskip, so the tests needing it SKIP there instead of "
        "failing:\n" + "\n".join(missing)
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
    production image that actually runs the tiled path.

    ``dask`` is exempt from this particular check: the tiled path never imports
    dask (``containers/tiled/requirements.txt`` doesn't pin it and never should
    — pinning it there would assert a dependency the tiled image doesn't have),
    so it cannot use this file as its authority. Its own check is
    ``test_dask_pin_is_harmonised_across_containers`` /
    ``test_ci_dask_pin_matches_harmonised_container_pin`` below.
    """
    tiled_pins = parse_tiled_requirements(TILED_REQUIREMENTS.read_text())
    tiled_scope = GUARDED_PACKAGES - NON_TILED_AUTHORITY

    assert tiled_pins.keys() >= tiled_scope, (
        f"{TILED_REQUIREMENTS.relative_to(ROOT)} does not pin all of "
        f"{sorted(tiled_scope)} — missing "
        f"{sorted(tiled_scope - tiled_pins.keys())}. This test's "
        "authority file must itself pin the full guarded set (dask excepted)."
    )

    mismatches = []
    for wf in workflow_files():
        for name, version in find_guarded_pins(wf.read_text()):
            if version is None:
                # Already reported by
                # test_ci_guarded_packages_are_pinned_with_double_equals.
                continue
            if name in NON_TILED_AUTHORITY:
                # Covered by that package's own authority check below --
                # test_ci_dask_pin_matches_harmonised_container_pin,
                # test_ci_matplotlib_pin_matches_the_segeval_image.
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


# dask is pinned container-side in three Dockerfiles -- NOT in
# containers/tiled/requirements.txt, since the tiled path never imports dask at
# all (see the exemption above) -- so it needs its own authority.
DASK_DOCKERFILES = [
    ROOT / "containers" / "convert" / "Dockerfile",
    ROOT / "containers" / "cellsam" / "Dockerfile",
    ROOT / "containers" / "stardist" / "Dockerfile",
]
_DOCKERFILE_DASK_PIN_RE = re.compile(r"\bdask(?:\[[A-Za-z0-9_,.-]+\])?==([A-Za-z0-9_.-]+)")


def parse_dockerfile_dask_pins() -> dict[str, str]:
    """{Dockerfile relpath: pinned dask version}, one entry per file in
    DASK_DOCKERFILES that pins dask at all."""
    pins: dict[str, str] = {}
    for path in DASK_DOCKERFILES:
        m = _DOCKERFILE_DASK_PIN_RE.search(path.read_text())
        if m:
            pins[path.relative_to(ROOT).as_posix()] = m.group(1)
    return pins


def test_dask_pin_is_harmonised_across_containers():
    """The three Dockerfiles that pin dask container-side must agree, or "the
    harmonised pin" (the premise the CI-side check below relies on) is a
    fiction."""
    pins = parse_dockerfile_dask_pins()
    assert set(pins) == {str(p.relative_to(ROOT)) for p in DASK_DOCKERFILES}, (
        f"expected every one of {[str(p.relative_to(ROOT)) for p in DASK_DOCKERFILES]} "
        f"to pin dask; found {pins}"
    )
    versions = set(pins.values())
    assert len(versions) == 1, f"dask pin diverges across containers: {pins}"


def test_ci_dask_pin_matches_harmonised_container_pin():
    """CI's/release's dask pin must match the harmonised container-side pin.

    Without this, dask can drift apart between the workflows and the
    containers exactly as silently as the class of bug Task 1 fixed an
    instance of for the other guarded packages — nothing else ties them
    together.
    """
    pins = parse_dockerfile_dask_pins()
    expected = next(iter(set(pins.values())))

    mismatches = []
    for wf in workflow_files():
        for name, version in find_guarded_pins(wf.read_text()):
            if name != "dask" or version is None:
                continue
            if version != expected:
                mismatches.append(
                    f"{wf.relative_to(ROOT)}: dask=={version} but the harmonised "
                    f"container-side pin is dask=={expected}"
                )

    assert not mismatches, (
        "workflow dask pin(s) disagree with the harmonised container-side pin "
        "(containers/convert, containers/cellsam, containers/stardist):\n"
        + "\n".join(sorted(set(mismatches)))
    )


# matplotlib's authority is containers/segeval/requirements.txt, NOT
# containers/tiled/requirements.txt: the tiled path never imports matplotlib, and
# segeval is the image that runs bin/seg_quality_eval.py -- the script
# tests/test_seg_quality_eval_cli.py drives, and the one that exits 1 without it.
#
# Unlike dask, matplotlib is deliberately NOT asserted to be harmonised across
# containers. regqc and quantify pin 3.9.2 for their own plotting; segeval pins
# 3.8.4 for the vendored CSE import. That divergence is real and pre-dates this
# guard, and flattening it would be a container change disguised as a test.
SEGEVAL_REQUIREMENTS = ROOT / "containers" / "segeval" / "requirements.txt"


def test_ci_matplotlib_pin_matches_the_segeval_image():
    """CI's matplotlib pin must match the image whose code path needs it.

    matplotlib is not a plotting nicety here. The vendored CSE source runs
    `import matplotlib.pyplot` at the top of functions.py's
    foreground_separation(), which single_method_eval.py calls on the main
    evaluation path -- so its absence is a hard failure of the scorer, which is
    exactly how it presented: bin/seg_quality_eval.py exiting 1 under CI while
    every developer machine that carried matplotlib passed.
    """
    pins = parse_tiled_requirements(SEGEVAL_REQUIREMENTS.read_text())
    expected = pins.get("matplotlib")
    assert expected, (
        f"{SEGEVAL_REQUIREMENTS.relative_to(ROOT)} no longer pins matplotlib, so "
        "this file has no authority to compare CI against. Either restore the pin "
        "or move matplotlib out of GUARDED_PACKAGES -- do not leave the authority "
        "dangling."
    )

    mismatches = []
    for wf in workflow_files():
        for name, version in find_guarded_pins(wf.read_text()):
            if name != "matplotlib" or version is None:
                continue
            if version != expected:
                mismatches.append(
                    f"{wf.relative_to(ROOT)}: matplotlib=={version} but "
                    f"{SEGEVAL_REQUIREMENTS.relative_to(ROOT)} pins "
                    f"matplotlib=={expected}"
                )
    assert not mismatches, "\n".join(sorted(set(mismatches)))


_DOCKERFILE_PIN_RE = r"(?<![\w.-]){}==([A-Za-z0-9_.-]+)"


def parse_tiled_dockerfile_code() -> str:
    """containers/tiled/Dockerfile with `#` comments stripped, via this module's own
    `_strip_comment` -- the same stripper `parse_tiled_requirements` already applies to
    the tiled and segeval requirements files.

    Not a nicety. Demonstrated green-while-broken before this was added: with

        # historical note: this image used to pin torch==2.3.1
        RUN ... pip install --no-cache-dir torch==2.4.0 ...

    the raw-text `re.search` below returned the FIRST match -- 2.3.1, out of the COMMENT --
    so the check agreed with CI's 2.3.1 and passed while the image would install 2.4.0.
    That is exactly the drift this check exists to catch. A guard a comment can satisfy is
    an annotation.
    """
    code = "\n".join(_strip_comment(line) for line in TILED_DOCKERFILE.read_text().splitlines())
    # A check whose "good" answer is an absence must prove it looked at something.
    assert "FROM python:" in code and "pip install" in code, (
        f"stripping comments from {TILED_DOCKERFILE.relative_to(ROOT)} left something "
        "that is not a Dockerfile any more; every pin lookup below would be searching an "
        "empty string and this check would pass vacuously"
    )
    return code


def test_ci_torch_and_kornia_pins_match_the_tiled_dockerfile():
    """torch/kornia are pinned in containers/tiled/Dockerfile rather than its
    requirements.txt -- torch needs a CPU --index-url and kornia must follow it -- so the
    Dockerfile is their authority. Without this check they could drift from the image CI
    claims to mirror, which is the exact class of bug this file exists to prevent."""
    docker_text = parse_tiled_dockerfile_code()
    expected = {}
    for pkg in ("torch", "kornia"):
        m = re.search(_DOCKERFILE_PIN_RE.format(pkg), docker_text)
        assert m, (
            f"{TILED_DOCKERFILE.relative_to(ROOT)} no longer pins {pkg}, so this check "
            "has no authority to compare CI against. Restore the pin rather than "
            "leaving the authority dangling."
        )
        expected[pkg] = m.group(1)

    compared = set()
    mismatches = []
    for wf in workflow_files():
        for name, version in find_guarded_pins(wf.read_text()):
            if name not in expected or version is None:
                continue
            compared.add(name)
            if version != expected[name]:
                mismatches.append(
                    f"{wf.relative_to(ROOT)}: {name}=={version} but "
                    f"{TILED_DOCKERFILE.relative_to(ROOT)} pins "
                    f"{name}=={expected[name]}"
                )

    # Non-vacuity: `mismatches` is empty both when the pins AGREE and when no workflow
    # pins either package at all. Only the first is a pass. `find_guarded_pins` is a
    # tokeniser over `pip install` lines, so a reworded install line (or one moved into a
    # requirements file) silently empties the comparison set and this assertion would
    # otherwise report success having compared nothing.
    assert compared == set(expected), (
        f"this check compared {sorted(compared)} against "
        f"{TILED_DOCKERFILE.relative_to(ROOT)}, but its authority covers "
        f"{sorted(expected)}. No workflow under "
        f"{WORKFLOWS_DIR.relative_to(ROOT)} pins {sorted(set(expected) - compared)} on a "
        "`pip install` line, so the comparison below is vacuous for it."
    )
    assert not mismatches, "\n".join(sorted(set(mismatches)))


def test_every_non_tiled_authority_package_has_its_own_check():
    """NON_TILED_AUTHORITY exempts a package from the tiled comparison. An entry
    with no replacement check exempts it from EVERY version comparison, which is
    strictly worse than not guarding it at all -- it would look covered.
    """
    src = Path(__file__).read_text()
    checks = {
        "dask": "test_ci_dask_pin_matches_harmonised_container_pin",
        "matplotlib": "test_ci_matplotlib_pin_matches_the_segeval_image",
        "torch": "test_ci_torch_and_kornia_pins_match_the_tiled_dockerfile",
        "kornia": "test_ci_torch_and_kornia_pins_match_the_tiled_dockerfile",
    }
    assert set(checks) == set(NON_TILED_AUTHORITY), (
        f"NON_TILED_AUTHORITY is {sorted(NON_TILED_AUTHORITY)} but this file maps "
        f"authority checks for {sorted(checks)}. Every exempted package needs one."
    )
    for pkg, fn in checks.items():
        assert f"def {fn}(" in src, (
            f"{pkg} is exempt from the tiled-authority check but its replacement "
            f"check {fn} does not exist"
        )
