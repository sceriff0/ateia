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

PHASE-2 NOTE. The old per-job checks read `run:` script bodies, so they would have gone
silently vacuous the moment those scripts moved into a composite action under
`.github/actions/**`. Assertion 1 still reads `run:` bodies -- it must, since that is where
a `pip install` can appear -- but it is a NEGATIVE rule: if the installs move, it goes on
finding nothing to complain about, which is correct, while assertions 2 and 3 (the ones
that carry the actual pins) read FILES and are unaffected by where the install command
lives.

Plain pytest, no pipeline-runtime imports, all paths derived from `__file__`.
"""

import ast
import importlib
import re
import shlex
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


def workflow_files() -> list[Path]:
    files = sorted(WORKFLOWS_DIR.glob("*.yml")) + sorted(WORKFLOWS_DIR.glob("*.yaml"))
    assert files, f"no workflow files found under {WORKFLOWS_DIR.relative_to(ROOT)}"
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

# Shell operators that end the argument list. Without them, `pip install -r x.txt && echo
# numpy==1.0` would put a version from the ECHO into the install's token list.
#
# Applied to shlex TOKENS, not to the raw string. A regex over the raw string cut
# `pip install "scipy >= 0, < 99"` at the ` < `, hiding a real quoted pin -- `<` is a
# redirect only when the shell sees it UNQUOTED, which is exactly the distinction shlex
# makes and a regex over the raw text cannot.
_SHELL_OPERATORS = frozenset({"&&", "||", ";", "|", "&", ">", ">>", "<", "<<", "2>", "2>&1"})

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
    return [m.group("args") for m in _PIP_INSTALL_RE.finditer(live)]


def split_args(args: str) -> list[str]:
    """Shell-split one pip argument string.

    `shlex`, not `str.split`. A plain split breaks on a QUOTED token containing spaces, so
    `pip install \'numpy == 1.0.0\'` -- which pip accepts, PEP 508 allows, and a human
    reviewing a diff reads as a pin -- came back as three tokens (`\'numpy`, `==`, `1.0.0\'`),
    none of which parse as a requirement. It was therefore invisible to BOTH halves of
    assertion 1: no version reported, and no "installs without -r/-c" reported either.

    A shell body that shlex cannot parse falls back to a naive split rather than raising at
    import time, but it does not go unnoticed:
    `test_every_workflow_pip_invocation_is_shell_parseable` fails on it by name.
    """
    try:
        toks = shlex.split(args, comments=True)
    except ValueError:
        toks = args.split()
    # Truncate at the first shell operator: everything after it is a different command.
    for i, tok in enumerate(toks):
        if tok in _SHELL_OPERATORS:
            return toks[:i]
    return toks


def requirement_tokens(args: str) -> list[str]:
    """The package tokens of one pip invocation: options, their values and paths removed."""
    tokens = []
    for tok in split_args(args):
        tok = tok.strip()
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


_FILE_OPTS = ("-r", "--requirement", "-c", "--constraint")


def option_targets(args: str, opts) -> list[str]:
    """Values of `-r`/`-c`-style options in one invocation, in either `-r X` or `-r=X` form."""
    toks = split_args(args)
    out = []
    for i, tok in enumerate(toks):
        if tok in opts:
            if i + 1 < len(toks):
                out.append(toks[i + 1])
            continue
        for o in opts:
            if tok.startswith(o + "="):
                out.append(tok[len(o) + 1:])
    return out


def goes_through_a_requirements_file(args: str) -> bool:
    return bool(option_targets(args, _FILE_OPTS))


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
        # A QUOTED requirement containing spaces. Legal pip, legal PEP 508, and invisible
        # to `str.split` -- which returned ["'numpy", "==", "1.0.0'"], none of which parse
        # as a requirement, so it was reported by NEITHER half of assertion 1. This case is
        # why split_args uses shlex.
        ("pip install 'numpy == 1.0.0'", ["numpy == 1.0.0"]),
        ('pip install "scipy >= 0, < 99"', ["scipy >= 0, < 99"]),
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


def test_the_workflows_actually_install_something():
    """An empty invocation set would make assertion 1 pass over nothing.

    This is not hypothetical bookkeeping: if a future refactor moves the installs somewhere
    this scanner cannot see, assertion 1 keeps passing while nothing is checked. Fail here
    instead, loudly, with the count.
    """
    counts = {
        wf.relative_to(ROOT).as_posix(): len(pip_invocations(wf.read_text()))
        for wf in workflow_files()
    }
    total = sum(counts.values())
    assert total >= 10, (
        "the workflows under .github/workflows/ contain almost no pip invocations "
        f"({counts}). Either the Python installs left CI, or they were reworded past "
        "_PIP_INSTALL_RE -- update the pattern rather than leaving the checks below to "
        "pass over an empty set."
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
    for wf in workflow_files():
        for args in pip_invocations(wf.read_text()):
            for tok in pinned_tokens(args):
                offenders.append(f"{wf.relative_to(ROOT).as_posix()}: `pip install ... {tok}`")
    assert not offenders, (
        "workflow YAML names a package version. Every version string belongs in "
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
    for wf in workflow_files():
        for args in pip_invocations(wf.read_text()):
            if goes_through_a_requirements_file(args):
                continue
            named = {Requirement(t).name.lower() for t in requirement_tokens(args)
                     if _parses(t)}
            extra = sorted(named - _BOOTSTRAP_ONLY)
            if extra:
                offenders.append(
                    f"{wf.relative_to(ROOT).as_posix()}: `pip install {' '.join(extra)}` "
                    "with no -r/-c"
                )
    assert not offenders, (
        "workflow YAML installs packages without reading a requirements/ file, so those "
        "packages are unpinned and invisible to Dependabot:\n  " + "\n  ".join(offenders)
    )


def _parses(token: str) -> bool:
    try:
        Requirement(token)
    except InvalidRequirement:
        return False
    return True


# --------------------------------------------------------------------------------------
# PRESENCE, PER JOB -- a different property from "no version is named here"
# --------------------------------------------------------------------------------------
# Assertions 1 and 3 are about WHERE a version lives. Neither says that the job which runs
# the suite actually installs anything, and that is a separate, load-bearing property with
# its own measured history.
#
# Measured 2026-08-31: deleting the imaging-stack install from `.github/workflows/ci.yml`'s
# `python-tests` job -- the blocking gate -- left the whole guard file GREEN, because five
# SIBLING JOBS in the same file each carried a `pip install` of their own to generate test
# data. A whole-FILE text scan let one job's install line satisfy the check on behalf of the
# job that needed it. A job runs on its own runner; another job's pip does nothing for it.
#
# So the unit here is the JOB, resolved with `yaml.safe_load` -- only the structural walk
# can say which `run:` belongs to which job -- and never a repo-wide grep, which is exactly
# the hole that fix closed.
#
# PHASE 2 (composite actions) IS ACCOUNTED FOR. `_job_run_text` follows any
# `uses: ./.github/actions/<name>` a job references and inlines that action's own `run:`
# steps, recursively. Moving `pip install -r requirements/ci.txt` into a composite action
# therefore keeps these checks live rather than silently emptying them -- which, without
# this, is precisely what would have happened: the DISK guard covers only torch/kornia and
# only per-FILE, and the blocking gate would have run with no cv2 (29 tests deselected in
# silence) and no pyyaml (which deletes both of tests/test_ci_job_hygiene.py's guards).
# --------------------------------------------------------------------------------------

# `pytest ... tests/` -- the DIRECTORY form, i.e. a job that runs the whole suite. The
# lookahead is what keeps `pytest tests/test_coarse_frontend.py` (the DISK non-skip proof
# step) from counting, which would let a job satisfy the scope with the proof step alone.
_RUNS_THE_PYTEST_SUITE_RE = re.compile(r"pytest\b[^\n]*\btests/(?=\s|$)")

ACTIONS_DIR = ROOT / ".github" / "actions"


def _local_action_steps(uses: str, yaml, seen: set[str]) -> list[dict]:
    """Steps of a local composite action referenced as `uses: ./path/to/action`.

    Returns [] for a third-party or remote action, which has no `run:` this repo owns.
    """
    if not uses.startswith("./") or uses in seen:
        return []
    seen.add(uses)
    base = ROOT / uses[2:]
    for name in ("action.yml", "action.yaml"):
        path = base / name if base.is_dir() else base
        if path.is_file():
            doc = yaml.safe_load(path.read_text()) or {}
            return (doc.get("runs") or {}).get("steps") or []
    return []


def _job_run_text(job: dict, yaml, seen: set[str] | None = None) -> str:
    """Every `run:` script a job executes, INCLUDING local composite actions it uses."""
    seen = set() if seen is None else seen
    parts = []
    for step in job.get("steps") or []:
        if not isinstance(step, dict):
            continue
        if "run" in step:
            parts.append(str(step["run"]))
        uses = step.get("uses")
        if isinstance(uses, str):
            nested = _local_action_steps(uses, yaml, seen)
            if nested:
                parts.append(_job_run_text({"steps": nested}, yaml, seen))
    return "\n".join(parts)


def jobs_running_the_pytest_suite() -> list[tuple[Path, str, str]]:
    """(workflow, job id, that job's effective `run:` text) for every JOB running the suite."""
    yaml = pytest.importorskip("yaml")
    hits: list[tuple[Path, str, str]] = []
    files_with_a_hit: set[Path] = set()
    for wf in workflow_files():
        jobs = (yaml.safe_load(wf.read_text()) or {}).get("jobs") or {}
        for job_id, job in jobs.items():
            if not isinstance(job, dict):
                continue
            run_text = _job_run_text(job, yaml)
            if _RUNS_THE_PYTEST_SUITE_RE.search(run_text):
                hits.append((wf, job_id, run_text))
                files_with_a_hit.add(wf)
    assert hits, (
        f"no JOB under {WORKFLOWS_DIR.relative_to(ROOT)} runs `pytest ... tests/`; the "
        "per-job checks below would pass over an empty set. Either the suite stopped "
        "running in CI, or the structural walk lost the step -- fix the walk, do not "
        "leave the checks passing over nothing."
    )
    # Non-vacuity, the other way round: the raw-text scan and the structural walk must
    # agree on WHICH FILES run the suite. A file whose text says `pytest ... tests/` but
    # none of whose jobs do means the YAML walk lost a step (a `run:` somewhere this
    # helper does not look, e.g. a composite action it could not resolve), and every
    # per-job assertion below silently stops covering that file.
    text_hits = {wf for wf in workflow_files()
                 if _RUNS_THE_PYTEST_SUITE_RE.search(wf.read_text())}
    lost = sorted(str(wf.relative_to(ROOT)) for wf in text_hits - files_with_a_hit)
    assert not lost, (
        f"these workflows contain a `pytest ... tests/` invocation in their raw text but "
        f"no JOB of theirs does: {lost}. The structural walk lost it, so the per-job "
        "checks below no longer cover those files."
    )
    return hits


def _requirements_installed_by(run_text: str) -> list[Path]:
    """Every `requirements/<file>` a shell body pip-installs with -r."""
    found = []
    for args in pip_invocations(run_text):
        for target in option_targets(args, ("-r", "--requirement")):
            path = REQUIREMENTS_DIR / Path(target).name
            if path.is_file() and path not in found:
                found.append(path)
    return found


def _declared_packages(path: Path, seen: set[Path] | None = None) -> set[str]:
    """Distribution names a requirements file declares, FOLLOWING its `-r` includes."""
    seen = set() if seen is None else seen
    if path in seen:
        return set()
    seen.add(path)
    names: set[str] = set()
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith(("-r", "--requirement")):
            target = line.split(None, 1)[1].strip() if " " in line else line.split("=", 1)[-1]
            nested = REQUIREMENTS_DIR / Path(target).name
            if nested.is_file():
                names |= _declared_packages(nested, seen)
            continue
        if line.startswith("-"):
            continue
        parsed = harmonisation._parse_req(line)
        if parsed:
            names.add(parsed[0])
    return names


def test_every_job_running_the_suite_installs_the_ci_requirements_file():
    """PER-JOB, structurally. The property the 2026-08-31 fix established, restored.

    `requirements/ci.txt` is the reviewed union the unit tests run in. A job that runs
    `pytest tests/` without installing it runs the suite in whatever the runner happened to
    ship -- and the suite does not fail, it DESELECTS: every module-scope
    `pytest.importorskip` turns into a silent skip. That is the shape of failure this whole
    file exists to make impossible.
    """
    missing = [
        f"{wf.relative_to(ROOT).as_posix()}: job `{job_id}`"
        for wf, job_id, run_text in jobs_running_the_pytest_suite()
        if REQUIREMENTS_DIR / "ci.txt" not in _requirements_installed_by(run_text)
    ]
    assert not missing, (
        "job(s) run `pytest ... tests/` without `pip install -r requirements/ci.txt`. Each "
        "of these jobs runs on its own runner, so a sibling job's -- or a sibling "
        "workflow's -- install line does nothing for it:\n  " + "\n  ".join(missing)
    )


# Import name -> distribution name, for the few that differ. Only these three do in this
# repo; anything else is assumed to match, and a mismatch fails the check below by name
# rather than passing silently.
_IMPORT_TO_DIST = {
    "yaml": "pyyaml",
    "cv2": "opencv-python-headless",
    "skimage": "scikit-image",
    "sklearn": "scikit-learn",
}


def module_scope_importorskips() -> dict[str, list[str]]:
    """{distribution: [test files]} for every MODULE-SCOPE pytest.importorskip under tests/.

    Parsed with `ast`, not a regex: a regex cannot tell a module-scope call from one inside
    a function, and the two mean different things here. A module-scope importorskip removes
    the WHOLE FILE from collection when its package is absent -- silently -- so the set of
    them is exactly the set of packages CI must install for the suite to mean anything.
    Derived from the suite itself, so a new importorskip is covered the moment it is written.
    """
    out: dict[str, list[str]] = {}
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - a broken test file fails elsewhere
            continue
        for node in tree.body:  # MODULE scope only: tree.body, never ast.walk
            for call in ast.walk(node) if isinstance(node, (ast.Assign, ast.Expr)) else ():
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "importorskip"
                    and call.args
                    and isinstance(call.args[0], ast.Constant)
                    and isinstance(call.args[0].value, str)
                ):
                    name = call.args[0].value
                    dist = _IMPORT_TO_DIST.get(name, name)
                    out.setdefault(dist, []).append(path.name)
    return out


def test_the_module_scope_importorskip_scan_is_not_empty():
    """Non-vacuity for the check below, which is a negative assertion over a derived set."""
    found = module_scope_importorskips()
    assert len(found) >= 8, (
        "found almost no module-scope pytest.importorskip under tests/ "
        f"({sorted(found)}). Either the suite stopped guarding its imports, or the AST walk "
        "no longer finds them -- fix the walk rather than leaving the check below to pass "
        "over an empty set."
    )
    # The three that must be there, because each is a package whose absence deselects tests
    # with nothing in the output to show for it, and each has bitten this repo.
    for dist in ("pyyaml", "opencv-python-headless", "kornia"):
        assert dist in found, (
            f"{dist} is no longer reached by the module-scope importorskip scan. It was "
            "found there when this check was written; if the suite genuinely stopped using "
            "it, remove it from this list deliberately."
        )


def test_every_job_running_the_suite_installs_what_the_suite_importorskips():
    """PER-JOB. `-r requirements/ci.txt` is necessary; this is what makes it sufficient.

    A job could install ci.txt and still be missing torch and kornia, which live in their
    own files for the CPU-index reason. And ci.txt itself could stop declaring pyyaml -- the
    package whose absence silently deletes both of tests/test_ci_job_hygiene.py's guards,
    and which reached the runner only as a transitive dependency of dask until 2026-08-31.

    The required set is DERIVED from the suite's own module-scope importorskips, so it
    cannot be forgotten out of: a new `pytest.importorskip` at module scope is covered the
    moment it is written, which is the same closed-rule property assertion 1 has.
    """
    required = module_scope_importorskips()
    missing = []
    for wf, job_id, run_text in jobs_running_the_pytest_suite():
        declared: set[str] = set()
        for req in _requirements_installed_by(run_text):
            declared |= _declared_packages(req)
        for dist, files in sorted(required.items()):
            if dist not in declared:
                missing.append(
                    f"{wf.relative_to(ROOT).as_posix()}: job `{job_id}`: no requirements "
                    f"file it installs declares {dist} -- "
                    f"{len(files)} test file(s) importorskip it at MODULE scope "
                    f"({', '.join(sorted(set(files))[:3])}...), so they are deselected "
                    "with nothing in the output to show for it"
                )
    assert not missing, (
        "job(s) run the pytest suite without installing something the suite guards with a "
        "module-scope importorskip. A skip is not a pass:\n  " + "\n  ".join(missing)
    )


def test_every_workflow_pip_invocation_is_shell_parseable():
    """`split_args` falls back to a naive split when shlex cannot parse; fail on that.

    Without this the fallback is a silent hole: an unbalanced quote anywhere in a pip line
    would send every token through the splitter that cannot see `'numpy == 1.0.0'`.
    """
    bad = []
    for wf in workflow_files():
        for args in pip_invocations(wf.read_text()):
            try:
                shlex.split(args)
            except ValueError as exc:
                bad.append(f"{wf.relative_to(ROOT).as_posix()}: `pip install{args}` -- {exc}")
    assert not bad, (
        "pip invocation(s) are not shell-parseable, so split_args falls back to a naive "
        "split and a quoted pin becomes invisible to assertion 1:\n  " + "\n  ".join(bad)
    )


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
        # ANCHORED on `requirements/constraints.txt`, not on any path ENDING in
        # "constraints.txt". The looser form accepted `-c /tmp/my-old-constraints.txt` --
        # a file that constrains nothing from requirements/ -- and passed assertion 2 while
        # the image's pins were unconstrained again. The Dockerfiles COPY to
        # /tmp/requirements/constraints.txt, so the anchored form matches all of them.
        if any(
            re.fullmatch(r"(?:.*/)?requirements/constraints\.txt", target)
            for target in option_targets(args, ("-c", "--constraint"))
        ):
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
        f"repository root; see .github/workflows/build-images.yml.)"
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


# Two DISTRIBUTION names that package the same upstream release. `containers/regqc` and
# `containers/merge` install `opencv-python`; CI installs `opencv-python-headless`, because
# the runners have no display libs. No single constraint can cover both -- pip resolves on
# the distribution name -- so both are pinned in constraints.txt and this table is what ties
# them together. Without it the two versions sat in two files with nothing between them: a
# CVE bump taking regqc to 4.11.x would have left CI exercising bin/utils/qc.py against a
# different cv2 than production ships, with nothing going red.
#
# The second name of each pair is exempt from the "installed by more than one image" rule,
# because by construction NO image installs it -- that is what makes its constraint inert
# and therefore free for the nine images that COPY the file.
EQUIVALENT_DISTRIBUTIONS = (("opencv-python", "opencv-python-headless"),)


def test_equivalent_distributions_are_pinned_to_the_same_release():
    """The tie the constraints file cannot express on its own."""
    pins = harmonisation.HARMONISED
    wrong = []
    for a, b in EQUIVALENT_DISTRIBUTIONS:
        for name in (a, b):
            assert name in pins, (
                f"requirements/constraints.txt no longer pins {name!r}, so the pair "
                f"({a}, {b}) is untied again and CI can test against a different build "
                f"than production ships. Pin it, or drop the pair from "
                f"EQUIVALENT_DISTRIBUTIONS with a reason."
            )
        if pins[a] != pins[b]:
            wrong.append(f"{a}=={pins[a]} vs {b}=={pins[b]}")
    assert not wrong, (
        "requirements/constraints.txt pins two packagings of the SAME upstream release at "
        "different versions. They must move together, or CI exercises a different build "
        "than the image ships: " + "; ".join(wrong)
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
    # The second name of an equivalent pair is installed by no image ON PURPOSE; its
    # constraint exists to hold CI to the first name's release and is inert everywhere else.
    exempt = {b for _a, b in EQUIVALENT_DISTRIBUTIONS}
    lonely = sorted(p for p, n in counts.items() if n < 2 and p not in exempt)
    assert not lonely, (
        f"requirements/constraints.txt constrains {lonely}, which fewer than two container "
        "images install. Move the pin into the one image's own requirements file, or into "
        "requirements/ci.txt if only CI needs it -- see the 'deliberately not constrained "
        "here' block at the bottom of constraints.txt for the four packages that are "
        "recorded rather than constrained, and why."
    )
