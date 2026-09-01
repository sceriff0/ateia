#!/usr/bin/env python3
"""Guard: there is exactly ONE copy of every dependency version, and it lives in requirements/.

WHAT THIS FILE USED TO BE, and why it is a tenth of the size now.

`.github/workflows/*.yml` used to write the imaging stack's pins inline --
`pip install numpy==1.26.4 scipy==1.15.3 ... zarr==2.18.3` -- and `containers/tiled/
requirements.txt` wrote the same numbers again. This file was 862 lines whose entire job
was proving the two copies equal: a hand-maintained `GUARDED_PACKAGES` set, a
`NON_TILED_AUTHORITY` sub-table for the four packages whose authority was a different
image, and one bespoke version-equality test per authority. It worked, and it was still
open at both ends. A package nobody added to `GUARDED_PACKAGES` was unguarded (that is how
CI shipped an unpinned `pandas`, and how `imagecodecs`, `matplotlib` and
`opencv-python-headless` each had to be added AFTER a real failure), and a new workflow job
could install a fifth version of anything.

The pins now live in `requirements/`. `requirements/constraints.txt` is the version
authority; the Dockerfiles COPY it and pass it to pip with `-c`; `requirements/ci.txt`
`-r`-includes the very files `containers/segeval` and `containers/tiled` build from. There
is no second copy to prove equal, so this file no longer tries. It asserts the three
properties that make the identity real:

  1. NO workflow names a package version at all, and every install goes through
     `-r requirements/<file>` (or `-c requirements/constraints.txt`). This is a CLOSED
     rule -- it needs no package list, so nothing can be forgotten out of it, and it is
     phrased over `pip install`, `pip3 install`, `python -m pip install` AND `uv pip
     install` so the planned switch to uv needs no change here.
  2. Every `containers/*/Dockerfile` that pip-installs a constrained package COPYs
     `requirements/constraints.txt` and passes it to every pip invocation that names one --
     unless it is a DOCUMENTED exception, and the exception list is derived from
     `test_container_harmonisation.PINNED_EXCEPTIONS`, not written by hand here.
  3. `requirements/constraints.txt` IS `test_container_harmonisation.HARMONISED`. That
     module reads the file rather than restating it; this asserts it still does, so
     re-hardcoding the table fails.

SCOPE: every workflow under `.github/workflows/` and every directory under `containers/`,
both discovered by glob -- never a filename list. The hardcoded ancestor of this guard
passed green while `release.yml` ran the identical unpinned install in its release gate.

PHASE-2 NOTE, WRITTEN BEFORE THE MOVE AND UPDATED AFTER IT. The old per-job checks read
`run:` script bodies, so they would have gone silently vacuous the moment those scripts
moved into a composite action under `.github/actions/**`. That move happened on
2026-09-01. Two consequences, both handled:

  * Assertion 1's SCOPE is now `.github/workflows/*.yml` AND `.github/actions/*/action.yml`
    (`tests/ci_actions.scanned_files()`). It is a negative rule, so leaving it pointed at
    the workflows alone would have kept it green while an unpinned `pip install numpy`
    sat in an action -- green for the reason that makes a guard worthless.
  * The non-vacuity check below no longer counts pip INVOCATIONS, because
    `.github/actions/setup-python-env` runs one `pip install -r "$req"` in a loop over the
    files its caller names. Counting invocations against a floor of 10 would fail on a
    correct tree. It counts what the loop actually installs instead, resolved PER JOB
    through `tests/ci_actions.py` -- which is a stronger statement than the old one, and
    the one that would actually notice the installs leaving CI.

Assertions 2 and 3 (the ones that carry the actual pins) read FILES and are unaffected by
where the install command lives.

Plain pytest, no pipeline-runtime imports, all paths derived from `__file__`.
"""

import importlib
import re
import sys
from pathlib import Path

import pytest
from packaging.requirements import InvalidRequirement, Requirement

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
CONTAINERS_DIR = ROOT / "containers"
REQUIREMENTS_DIR = ROOT / "requirements"
CONSTRAINTS = REQUIREMENTS_DIR / "constraints.txt"

# The harmonisation module owns the Dockerfile parser and the documented per-image
# exceptions. Importing it rather than re-deriving either is the point: seven guards in this
# repo once carried seven private parses of the same files and each had a different blind
# spot. sys.path is nudged explicitly so this import does not depend on pytest's rootdir
# insertion behaviour.
if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))
harmonisation = importlib.import_module("test_container_harmonisation")
ci_actions = importlib.import_module("ci_actions")


def workflow_files() -> list[Path]:
    files = sorted(WORKFLOWS_DIR.glob("*.yml")) + sorted(WORKFLOWS_DIR.glob("*.yaml"))
    assert files, f"no workflow files found under {WORKFLOWS_DIR.relative_to(ROOT)}"
    return files


def scanned_files() -> list[Path]:
    """Every file a CI `pip install` can live in: workflows AND composite actions.

    Widened 2026-09-01. `.github/actions/setup-python-env` is where every CI install now
    happens; scanning only the workflows would leave assertion 1 -- a negative rule --
    passing over a directory that no longer contains an install at all.
    """
    files = ci_actions.scanned_files()
    assert files, "no workflow or composite-action files found under .github/"
    return files


def container_dirs() -> list[str]:
    dirs = sorted(p.name for p in CONTAINERS_DIR.iterdir() if (p / "Dockerfile").is_file())
    assert dirs, f"no Dockerfiles found under {CONTAINERS_DIR.relative_to(ROOT)}"
    return dirs


# --------------------------------------------------------------------------------------
# Parsing a pip invocation out of a shell body
# --------------------------------------------------------------------------------------
# `uv pip install` is accepted deliberately, ahead of the installer switch: the rule is
# about where the VERSION lives, not about which binary resolves it, and a guard that had to
# be edited to allow uv would be one more reason to edit a guard while changing what it
# guards.
_PIP_INSTALL_RE = re.compile(
    r"(?:uv\s+)?(?:python[0-9.]*\s+-m\s+)?pip[0-9]*\s+install\b(?P<args>[^\n]*)"
)

# Shell operators that end the argument list. Without these, `pip install -r x.txt && echo
# numpy==1.0` would put a version from the ECHO into the install's token list.
_ARG_TERMINATORS = re.compile(r"\s(?:&&|\|\||;|\||>|<)\s|\s#")

# Bootstrap tooling pip installs of itself. These may appear without `-r`/`-c` because there
# is nothing to pin them to and no image ships a version of them that CI must match. They
# may still not carry a VERSION -- assertion 1 applies to them like everything else.
_BOOTSTRAP_ONLY = frozenset({"pip", "setuptools", "wheel"})


def pip_invocations(text: str) -> list[str]:
    """Every pip/uv-pip install argument string in a shell body, comments excluded.

    Continuation backslashes are joined first, so a multi-line install is one invocation.
    Whole-line `#` comments are dropped BEFORE the search: a comment explaining why a pin
    was removed necessarily quotes the pin, and reading that as a live install would
    pressure the next person to delete the rationale instead of keeping it.
    """
    joined = re.sub(r"\\\s*\n", " ", text)
    live = "\n".join(ln for ln in joined.splitlines() if not ln.strip().startswith("#"))
    out = []
    for m in _PIP_INSTALL_RE.finditer(live):
        args = m.group("args")
        cut = _ARG_TERMINATORS.search(args)
        out.append(args[: cut.start()] if cut else args)
    return out


def requirement_tokens(args: str) -> list[str]:
    """The package tokens of one pip invocation: options, their values and paths removed."""
    tokens = []
    for tok in args.split():
        tok = tok.strip().strip('"').strip("'")
        if not tok:
            continue
        # Options, option values that are URLs or paths, shell variables, and VCS installs.
        if tok.startswith(("-", "$", "http://", "https://", "git+")) or "/" in tok:
            continue
        tokens.append(tok)
    return tokens


def pinned_tokens(args: str) -> list[str]:
    """Tokens of one invocation that name a package AND a version specifier."""
    pinned = []
    for tok in requirement_tokens(args):
        try:
            req = Requirement(tok)
        except InvalidRequirement:
            continue
        if str(req.specifier):
            pinned.append(tok)
    return pinned


def goes_through_a_requirements_file(args: str) -> bool:
    return bool(re.search(r"(?:^|\s)(?:-r|--requirement|-c|--constraint)[=\s]", args))


# --------------------------------------------------------------------------------------
# Non-vacuity: the detector must actually detect
# --------------------------------------------------------------------------------------
# Every check below is a NEGATIVE assertion over a discovered set. Two ways such a guard
# passes having proved nothing -- the set comes back empty, or the detector never matches
# the form it is looking for -- and this repo has been bitten by both. These two tests are
# what make the three real ones mean something.


@pytest.mark.parametrize(
    "body,expected",
    [
        ("pip install numpy==1.0.0", ["numpy==1.0.0"]),
        ("pip3 install 'dask[array]==2026.7.1'", ["dask[array]==2026.7.1"]),
        ("python -m pip install anndata<0.12", ["anndata<0.12"]),
        ("uv pip install --system tifffile==2025.5.10", ["tifffile==2025.5.10"]),
        ("pip install torch==2.3.1 --index-url https://download.pytorch.org/whl/cpu",
         ["torch==2.3.1"]),
        # Must NOT fire on any of these.
        ("pip install -r requirements/ci.txt", []),
        ("uv pip install --system -r requirements/ci.txt", []),
        ("python -m pip install --upgrade pip setuptools wheel", []),
        ("pip install -c /tmp/requirements/constraints.txt numpy", []),
        ("  # pip install numpy==1.0.0  -- a comment, not an install", []),
        ("pip install -r requirements/ci.txt && echo numpy==9.9.9", []),
    ],
)
def test_the_version_detector_actually_detects(body, expected):
    """The exact forms this repo writes, plus the three ways it has fooled a scanner before.

    The trailing three are the near-misses: a `#` comment quoting a removed pin, a version
    appearing AFTER a shell operator, and an option value (`--index-url https://...`) that a
    naive splitter reads as a package name.
    """
    found = [tok for args in pip_invocations(body) for tok in pinned_tokens(args)]
    assert found == expected


def test_the_scanned_files_actually_install_something():
    """An empty invocation set would make assertion 1 pass over nothing.

    This is not hypothetical bookkeeping: a refactor that moves the installs somewhere this
    scanner cannot see leaves assertion 1 passing while nothing is checked. Fail here
    instead, loudly, with the count. It happened on 2026-09-01 -- the installs moved into
    `.github/actions/**`, which is why `scanned_files()` reaches there now.
    """
    counts = {
        p.relative_to(ROOT).as_posix(): len(pip_invocations(p.read_text()))
        for p in scanned_files()
    }
    total = sum(counts.values())
    assert total >= 1, (
        "no file under .github/workflows/ or .github/actions/ contains a pip invocation "
        f"({counts}). Either the Python installs left CI, or they were reworded past "
        "_PIP_INSTALL_RE -- update the pattern rather than leaving the checks below to "
        "pass over an empty set."
    )


# The requirements/ files CI actually installs, as a NAMED SET rather than a count.
# Measured 2026-09-01 on ci/phase-6-7 (CI redesign Phase 7):
#   _test-suite.yml::python-tests    ci.txt, torch-cpu.txt, kornia.txt
#   _test-suite.yml::nextflow-stub   testdata.txt
#   _test-suite.yml::nf-test-stub    testdata.txt
#   _test-suite.yml::security-tests  testdata.txt
#   _test-suite.yml::ruff            lint.txt
#   nightly.yml::nf-test-real        testdata.txt
#   nightly.yml::nf-test-integration testdata.txt
# requirements/nf-core.txt was in this set until Phase 7 deleted the `nf-core lint`
# job that installed it; no released nf-core/tools version can lint this repository
# (see .nf-core.yml's header), so the job went rather than the pin.
EXPECTED_CI_REQUIREMENTS = {
    "requirements/ci.txt",
    "requirements/torch-cpu.txt",
    "requirements/kornia.txt",
    "requirements/testdata.txt",
    "requirements/lint.txt",
}


def test_ci_still_installs_the_files_it_is_supposed_to():
    """The real non-vacuity floor, and the reason the invocation count above can be 1.

    `.github/actions/setup-python-env` installs `pip install -r "$req"` in a loop, so
    counting invocations no longer measures anything: one loop covers three files in
    `python-tests` and one in `ruff`. What matters is which requirements/ files each JOB
    ends up installing, which `tests/ci_actions.py` resolves through the `requirements:`
    input at each call site.

    PER JOB, not a union over the repository. A union is how a sibling job's install line
    came to satisfy a check on behalf of the job that actually needed the package.
    """
    per_job = {}
    for wf in workflow_files():
        for name, job in ci_actions.load_jobs(wf).items():
            installed = ci_actions.requirement_files_installed(job)
            if installed:
                per_job[f"{wf.relative_to(ROOT).as_posix()}::{name}"] = installed

    distinct = {req for files in per_job.values() for req in files}

    # TWO CHECKS, NOT ONE, AND THE REASON IS THE MESSAGE RATHER THAN THE NUMBER.
    # This was a bare `len(distinct) >= 5`, and after Phase 7 deleted the nf-core lint
    # job (and requirements/nf-core.txt with it) the resolved set is EXACTLY five. So
    # the next routine consolidation -- folding requirements/kornia.txt into
    # requirements/testdata.txt, say, which changes no behaviour -- would have gone red
    # saying "the installs have gone somewhere tests/ci_actions.py cannot follow",
    # which is not what happened and sends the reader to the wrong file.
    #
    # The tripwire and the inventory are different questions, so they are asked
    # separately: a floor far below any plausible consolidation catches a resolver
    # that stopped resolving, and an exact set catches a change to WHICH files CI
    # installs -- naming the difference in both directions, so the failure text says
    # what actually changed.
    assert len(distinct) >= 3, (
        f"CI resolves to only {len(distinct)} distinct requirements/ file(s) across "
        "every job. That is far below anything a consolidation would produce, so the "
        "installs have gone somewhere tests/ci_actions.py cannot follow -- fix the "
        f"resolver, not this number: {per_job}"
    )
    if distinct != EXPECTED_CI_REQUIREMENTS:
        gone = sorted(EXPECTED_CI_REQUIREMENTS - distinct)
        new_files = sorted(distinct - EXPECTED_CI_REQUIREMENTS)
        raise AssertionError(
            "the set of requirements/ files CI installs has changed.\n"
            f"  no longer installed: {gone or 'none'}\n"
            f"  newly installed:     {new_files or 'none'}\n"
            "If a file was CONSOLIDATED into another or a job was deleted, that is a "
            "normal change: update EXPECTED_CI_REQUIREMENTS in the same commit and "
            "this test is done. If a file went missing with no such change, a job "
            "stopped installing something it needs -- and a missing pin is how this "
            "suite once ran with no cv2 and 29 tests silently deselected.\n"
            f"Per job: {per_job}"
        )
    for req in sorted(distinct):
        assert (REQUIREMENTS_DIR / Path(req).name).is_file(), (
            f"a CI job installs {req}, which does not exist. Either the file was deleted "
            "or the call site names it wrongly -- and a `pip install -r` of a missing "
            "file fails the job, so this is a red build waiting to happen."
        )


def test_no_workflow_step_that_installs_a_requirements_file_is_conditional():
    """`tests/ci_actions.py` evaluates an `if:` inside a composite action and ignores one
    on a workflow step -- it has no way to resolve `always()` or a matrix expression.

    That asymmetry is safe only while no workflow step gates an install. Assert it, rather
    than leaving the resolver quietly over-reporting the day someone adds `if:` to an
    install step.
    """
    offenders = []
    for wf in workflow_files():
        for name, job in ci_actions.load_jobs(wf).items():
            for step in ci_actions.steps_of(job):
                if "if" not in step:
                    continue
                if ci_actions._requirements_from_step(step):
                    offenders.append(
                        f"{wf.relative_to(ROOT).as_posix()}::{name}: step "
                        f"{step.get('name', step.get('uses', '?'))!r} installs a "
                        f"requirements file behind `if: {step['if']}`"
                    )
    assert not offenders, (
        "a workflow step installs a requirements/ file conditionally. "
        "tests/ci_actions.requirement_files_installed does not evaluate workflow-level "
        "`if:` expressions, so it would report that install as unconditional:\n  "
        + "\n  ".join(offenders)
    )


# --------------------------------------------------------------------------------------
# Assertion 1 -- no workflow names a version; every install reads a file
# --------------------------------------------------------------------------------------


def test_no_workflow_pins_a_package_version():
    """A CLOSED rule: no package list, so no package can be forgotten out of it.

    This replaces a hand-maintained `GUARDED_PACKAGES` set that had to be extended three
    separate times AFTER a real CI failure caused by the package it did not yet name.
    """
    offenders = []
    for path in scanned_files():
        for args in pip_invocations(path.read_text()):
            for tok in pinned_tokens(args):
                offenders.append(f"{path.relative_to(ROOT).as_posix()}: `pip install ... {tok}`")
    assert not offenders, (
        "workflow or composite-action YAML names a package version. Every version string belongs in "
        "requirements/ -- constraints.txt for anything a container image also installs, "
        "or the per-job file otherwise -- so the pin exists once and Dependabot can see "
        "it. Offenders:\n  " + "\n  ".join(offenders)
    )


def test_every_workflow_install_goes_through_a_requirements_file():
    """The positive half of the same rule.

    Assertion 1 alone permits `pip install numpy` -- unpinned, no file, and silently
    resolving whatever PyPI serves that day, which is the exact failure that started this
    whole guard (an unpinned tifffile pulling a zarr>=3.2 that was then force-downgraded,
    leaving CI reading blank thumbnails). An install must name a file, or install nothing
    but pip's own bootstrap tooling.
    """
    offenders = []
    for path in scanned_files():
        for args in pip_invocations(path.read_text()):
            if goes_through_a_requirements_file(args):
                continue
            named = {Requirement(t).name.lower() for t in requirement_tokens(args)
                     if _parses(t)}
            extra = sorted(named - _BOOTSTRAP_ONLY)
            if extra:
                offenders.append(
                    f"{path.relative_to(ROOT).as_posix()}: `pip install {' '.join(extra)}` "
                    "with no -r/-c"
                )
    assert not offenders, (
        "workflow or composite-action YAML installs packages without reading a requirements/ file, so those "
        "packages are unpinned and invisible to Dependabot:\n  " + "\n  ".join(offenders)
    )


def _parses(token: str) -> bool:
    try:
        Requirement(token)
    except InvalidRequirement:
        return False
    return True


# --------------------------------------------------------------------------------------
# Assertion 2 -- every image that installs a constrained package reads the constraints file
# --------------------------------------------------------------------------------------
# The exception list is DERIVED, never written here. `PINNED_EXCEPTIONS` already records
# every (image, package) pair that must diverge from the harmonised set, each with the
# upstream constraint forcing it; an image on that list literally cannot take the
# constraints file, because pip reports ResolutionImpossible rather than letting a
# constraint "win" over a divergent direct requirement. Deriving the list means a new
# exception costs a documented PINNED_EXCEPTIONS entry -- it cannot be bought with a name
# added to a list in this file.
EXEMPT = frozenset({harmonisation.PY312_ISLAND}) | {
    container for container, _pkg in harmonisation.PINNED_EXCEPTIONS
}


def constrained_packages() -> set[str]:
    return set(harmonisation.HARMONISED)


def _dockerfile_text(container: str) -> str:
    return (CONTAINERS_DIR / container / "Dockerfile").read_text()


def _constrained_installs(container: str) -> set[str]:
    """Constrained packages the image installs, via the harmonisation module's own parser."""
    text = _dockerfile_text(container)
    tokens = harmonisation._dockerfile_pip_tokens(text)
    for req in harmonisation._requirements_files_installed(text):
        tokens += harmonisation._requirements_tokens(req)
    names = set()
    for tok in tokens:
        parsed = harmonisation._parse_req(tok)
        if parsed:
            names.add(parsed[0])
    return names & constrained_packages()


def _uncovered_invocations(container: str) -> list[str]:
    """pip invocations naming a constrained package that no constraints file reaches.

    An invocation is covered either by `-c <...>constraints.txt` on the command line, or by
    installing a `requirements/<name>.txt` that itself opens with `-c constraints.txt` (pip
    resolves that path relative to the file naming it, which is why the Dockerfiles COPY
    both into one directory).
    """
    text = _dockerfile_text(container)
    uncovered = []
    for args in pip_invocations(text):
        names = set()
        for tok in requirement_tokens(args):
            parsed = harmonisation._parse_req(tok)
            if parsed:
                names.add(parsed[0])
        if not names & constrained_packages():
            continue
        if re.search(r"(?:-c|--constraint)[=\s]+\S*constraints\.txt", args):
            continue
        via_file = any(
            re.search(r"(?m)^\s*-c\s+constraints\.txt\s*$", req.read_text())
            for req in harmonisation._requirements_files_installed(args)
        )
        if via_file:
            continue
        uncovered.append(f"pip install{args}")
    return uncovered


@pytest.mark.parametrize("container", container_dirs())
def test_image_installing_a_constrained_package_reads_the_constraints_file(container):
    installs = sorted(_constrained_installs(container))
    if not installs:
        pytest.skip(f"containers/{container} installs no constrained package")
    if container in EXEMPT:
        pytest.skip(
            f"containers/{container} is a documented exception "
            f"(test_container_harmonisation.PINNED_EXCEPTIONS / PY312_ISLAND)"
        )
    text = _dockerfile_text(container)
    assert re.search(r"COPY[^\n]*requirements/constraints\.txt", text), (
        f"containers/{container}/Dockerfile installs {installs}, which requirements/"
        f"constraints.txt pins, but never COPYs that file -- so the image's pins are a "
        f"second copy of the version authority again. Add "
        f"`COPY requirements/constraints.txt /tmp/requirements/constraints.txt` and pass "
        f"`-c /tmp/requirements/constraints.txt` to pip. (The build context is the "
        f"repository root; see .github/workflows/containers.yml.)"
    )
    uncovered = _uncovered_invocations(container)
    assert not uncovered, (
        f"containers/{container}/Dockerfile COPYs requirements/constraints.txt but does "
        f"not pass it to every pip invocation that names a constrained package, so those "
        f"pins are unconstrained:\n  " + "\n  ".join(uncovered)
    )


@pytest.mark.parametrize("container", sorted(EXEMPT))
def test_every_exempt_image_is_still_diverging(container):
    """A stale exemption is worse than none: it reads as a reviewed decision.

    An image is exempt only while it genuinely cannot take the constraints file. The moment
    its divergence is resolved upstream, the exemption must go -- and the way to find that
    out is to fail here, not to leave the image quietly outside the guard forever.
    """
    installs = _constrained_installs(container)
    assert installs, (
        f"containers/{container} is exempt from the constraints file but installs no "
        f"constrained package at all, so the exemption buys nothing. Remove it from "
        f"test_container_harmonisation.PINNED_EXCEPTIONS."
    )
    text = _dockerfile_text(container)
    assert not re.search(r"COPY[^\n]*requirements/constraints\.txt", text), (
        f"containers/{container} is listed as an exception yet now COPYs requirements/"
        f"constraints.txt. If the divergence is gone, delete its PINNED_EXCEPTIONS "
        f"entries; if it is not, the build will fail with ResolutionImpossible."
    )


# --------------------------------------------------------------------------------------
# Assertion 3 -- the constraints file IS the harmonised table
# --------------------------------------------------------------------------------------


def test_the_constraints_file_is_the_harmonised_table():
    """`test_container_harmonisation.HARMONISED` must be READ from the file, not restated.

    This is the assertion that makes the other two worth having. If the table is ever
    hardcoded again, the images get checked against a set of numbers that nothing installs,
    and both the Dockerfiles and CI can drift from it without anything going red.
    """
    fresh = harmonisation._read_constraints(CONSTRAINTS)
    assert harmonisation.HARMONISED == fresh, (
        "tests/test_container_harmonisation.py's HARMONISED table no longer equals "
        "requirements/constraints.txt. It must READ that file -- the same file the "
        "Dockerfiles COPY and pip enforces with `-c` -- not restate it:\n"
        f"  HARMONISED  = {harmonisation.HARMONISED}\n"
        f"  constraints = {fresh}"
    )


def test_every_constrained_package_is_installed_by_more_than_one_image():
    """The constraints file is for SHARED packages; a solo pin belongs in that image's file.

    Without this, constraints.txt grows into a second global requirements file, and every
    image that COPYs it starts carrying resolution pressure from packages it never installs.
    """
    counts = {pkg: 0 for pkg in constrained_packages()}
    for container in container_dirs():
        for pkg in _constrained_installs(container):
            counts[pkg] += 1
    lonely = sorted(p for p, n in counts.items() if n < 2)
    assert not lonely, (
        f"requirements/constraints.txt constrains {lonely}, which fewer than two container "
        "images install. Move the pin into the one image's own requirements file, or into "
        "requirements/ci.txt if only CI needs it -- see the 'deliberately not constrained "
        "here' block at the bottom of constraints.txt for the four packages that are "
        "recorded rather than constrained, and why."
    )
