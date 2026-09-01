"""The DISK front-end test must actually RUN in CI, not skip.

tests/test_coarse_frontend.py guards its DISK+LightGlue cases with
importorskip("torch")/importorskip("kornia"). Before this task CI had no torch, so
those cases skipped silently -- and a silent skip is how an unimplemented front-end
shipped in the first place.

SCOPE IS PER-JOB, DELIBERATELY. An earlier version of this guard concatenated
*every* file under `.github/workflows/` and asserted against the union. That is a
mask, not a scope: deleting BOTH torch and kornia installs from
`.github/workflows/ci.yml` -- the workflow whose `python-tests` job is CI's blocking
gate -- left `release.yml`'s copy standing, and every assertion here passed (10
passed). In that state the DISK cases importorskip away in the gate that actually
runs on every push, which is precisely the regression this file exists to catch.
Every job that runs the pytest suite is therefore checked ON ITS OWN.

WHY THAT NOW RESOLVES TO ONE JOB, AND WHY THAT IS NOT A WEAKENING. Since
2026-09-01 (CI redesign Phase 5) the suite runs in ONE place --
`.github/workflows/_test-suite.yml`'s `python-tests` job -- which `ci.yml` and
`release.yml` both call as a reusable workflow. The union-vs-per-file failure this
file was written against was two COPIES disagreeing; there is no second copy to
disagree. The per-job form is kept, unchanged, because it is the form that stays
correct if a second pytest job ever appears: `jobs_running_the_pytest_suite()`
discovers by COMMAND across every workflow, so the day one does, it is checked on
its own without an edit here. If that discovery ever returns nothing, the
non-vacuity assertion in it fails rather than this file going quietly green.

THE VERSIONS ARE NO LONGER READ OUT OF THE WORKFLOW. They live in
`requirements/torch-cpu.txt` and `requirements/kornia.txt`, which are the SAME files
`containers/tiled/Dockerfile` installs -- one copy, not two. So this file asserts that
each in-scope workflow installs those two files, in that order, and separately that
those files still pin torch and kornia and still use the CPU index. Asserting the
version string against the workflow text would put the second copy back.

The workflow set is DISCOVERED, not hardcoded: any workflow whose `run:` block
invokes pytest over the `tests/` directory is in scope the moment it is added. A
filename list here would have the opposite failure mode -- a new workflow that runs
the suite without torch would go unnoticed.

AND THE NON-SKIP CLAIM IS ASSERTED, NOT MERELY DOCUMENTED. Installing torch is
necessary but not sufficient: a wheel can install and still fail to import (a
CUDA-only build on a CPU runner, an ABI mismatch with the pinned numpy), and
importorskip swallows exactly that. This file's docstring -- and
tests/test_coarse_frontend.py's -- used to say it "asserts this test is NOT
skipped" while asserting nothing of the kind. So each in-scope workflow must also
carry a step that (a) imports torch and kornia in the same interpreter pytest runs
in and (b) runs tests/test_coarse_frontend.py and fails on any reported skip.

SCOPE WIDENED 2026-09-01 (CI redesign, Phase 2), AND NARROWED IN THE SAME BREATH.
The install steps moved into `.github/actions/setup-python-env`, so a scan that
stops at `.github/workflows/*.yml` no longer sees a `pip install` line at all —
it would have gone green over a workflow that installs nothing. Two changes:

  * the install check is resolved through `tests/ci_actions.py`, which follows a
    job's `uses: ./.github/actions/...` steps into the action and reads the
    `requirements:` input the job passes. Remove that resolution and this file
    reports "no torch/kornia install" for both workflows — verified by doing it.
  * it is now checked PER JOB, not per FILE. Per-file was already the fix for one
    round of this defect (a sibling FILE covering for the one that mattered); a
    sibling JOB inside the same file could still do it, and after the move the
    only thing tying a requirements file to a job is the `with:` block at the
    call site. `.github/actions/setup-python-env` declares `requirements` with NO
    DEFAULT for exactly this reason: a file cannot be installed without some job
    naming it.

The non-skip proof steps still live in the workflows, but they are read through
the same resolver so that moving them later cannot silently blind this file.

Plain pytest, no pipeline-runtime imports, all paths derived from `__file__`, per
this repo's other guard tests.
"""

import importlib
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The shared workflow/composite-action resolver. Imported rather than re-derived:
# seven guards in this repo once carried seven private parses of the same files
# and each had a different blind spot.
if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))
ci_actions = importlib.import_module("ci_actions")
WORKFLOWS = REPO / ".github" / "workflows"

# `pytest ... tests/` -- the DIRECTORY form, i.e. the whole suite. The trailing
# lookahead for whitespace/end is what keeps `pytest tests/test_coarse_frontend.py`
# (the non-skip proof step this file requires below) from counting as "runs the
# suite", which would let a workflow satisfy the scope test with the proof step alone.
_RUNS_THE_SUITE_RE = re.compile(r"pytest\b[^\n]*\btests/(?=\s|$)")

REQUIREMENTS = REPO / "requirements"
TORCH_REQ = REQUIREMENTS / "torch-cpu.txt"
KORNIA_REQ = REQUIREMENTS / "kornia.txt"

# The install is `pip install -r requirements/torch-cpu.txt`. Matching the FILE rather
# than the version is what keeps a single copy of the pin: the file is read by CI and by
# containers/tiled/Dockerfile alike.
_TORCH_INSTALL_RE = re.compile(r"pip install[^\n]*\brequirements/torch-cpu\.txt\b")
_KORNIA_INSTALL_RE = re.compile(r"pip install[^\n]*\brequirements/kornia\.txt\b")

# The two halves of the non-skip proof. Both must be present: the import check alone
# would pass in a job that never runs the file, and the skip check alone would not
# distinguish "imported fine" from "collected nothing".
_IMPORT_PROOF_RE = re.compile(r"python -c [\"']import torch, kornia")
_NO_SKIP_PROOF_RE = re.compile(
    r"pytest[^\n]*tests/test_coarse_frontend\.py[\s\S]{0,400}?grep -qi 'skipped'"
)


def workflow_files():
    return ci_actions.workflow_files()


def jobs_running_the_pytest_suite():
    """Every (workflow, job name, job) that runs the Python unit-test suite.

    Discovered by looking for a `pytest ... tests/` invocation in the job's own
    `run:` bodies and in any local composite action it uses — never by filename
    and never by job name.

    COMMENT-BLIND (`ci_actions.job_run_scripts`, not `job_resolved_run_text`).
    Both questions this file asks — "does this job run the suite" and "does it
    carry the non-skip proof" — are command-runs claims, and on the raw view a
    comment quoting the command answers them. A commented-out proof step is the
    likelier way this coverage is lost than a deleted one.
    """
    hits = []
    for wf in workflow_files():
        for name, job in ci_actions.load_jobs(wf).items():
            if _RUNS_THE_SUITE_RE.search(ci_actions.job_run_scripts(job)):
                hits.append((wf, name, job))
    # Non-vacuity: an empty list would make every check below pass having
    # examined nothing, which is the failure mode this whole file is about.
    assert hits, (
        f"no job under {WORKFLOWS.relative_to(REPO)} runs `pytest ... tests/`. "
        "Either the suite stopped running in CI, or the invocation was reworded past "
        "this detector -- update _RUNS_THE_SUITE_RE rather than leaving the checks "
        "below to pass over an empty set."
    )
    return hits


def workflows_running_the_pytest_suite():
    """The FILES containing such a job, de-duplicated and order-preserving."""
    out = []
    for wf, _name, _job in jobs_running_the_pytest_suite():
        if wf not in out:
            out.append(wf)
    return out


def test_every_pytest_job_installs_torch_and_kornia():
    """PER JOB, resolved through the composite actions the job calls.

    Per-file was the previous fix and it is no longer enough: after the Phase-2
    move the install is a `requirements:` input at the call site, so the thing
    that ties torch to the job that needs it IS the job's own `with:` block. A
    sibling job passing it does not help this one.
    """
    missing = []
    for wf, name, job in jobs_running_the_pytest_suite():
        installed = ci_actions.requirement_files_installed(job)
        where = f"{wf.relative_to(REPO)}: job `{name}`"
        for req in ("requirements/torch-cpu.txt", "requirements/kornia.txt"):
            if req not in installed:
                missing.append(f"{where} does not install {req} (installs {installed})")
        if "requirements/torch-cpu.txt" in installed and "requirements/kornia.txt" in installed:
            if installed.index("requirements/torch-cpu.txt") > installed.index(
                "requirements/kornia.txt"
            ):
                missing.append(
                    f"{where} installs kornia BEFORE torch. kornia drags the ~2.5 GB "
                    "CUDA wheel from PyPI if torch is not already satisfied, so the "
                    "order is load-bearing, not stylistic."
                )
    assert not missing, (
        "job(s) run the pytest suite without installing the DISK front-end "
        "stack, so tests/test_coarse_frontend.py's DISK cases importorskip away "
        "there and prove nothing:\n" + "\n".join(missing)
    )


def test_the_install_detector_still_matches_a_literal_pip_install():
    """Non-vacuity for the two module-level regexes, which no longer fire on any
    workflow now that the installs are a `with: requirements:` input.

    They are kept because a job may go back to writing `pip install -r ...`
    inline at any time, and `ci_actions.requirement_files_installed` reads that
    form too. A regex that matches nothing anywhere is indistinguishable from a
    regex that is broken, so it is exercised here against the literal form.
    """
    sample = "pip install -r requirements/torch-cpu.txt\npip install -r requirements/kornia.txt"
    assert _TORCH_INSTALL_RE.search(sample)
    assert _KORNIA_INSTALL_RE.search(sample)


def test_every_pytest_workflow_installs_the_cpu_torch_wheel():
    """A CUDA wheel is ~2.5 GB and pointless on a CPU runner; the shipped container is
    CPU-only, so every workflow must exercise the same wheel.

    The index-url now lives in requirements/torch-cpu.txt, so the workflow proves it by
    installing that file (checked above) and this asserts the file still carries it. That
    is one copy, shared with containers/tiled/Dockerfile, instead of one per workflow.
    """
    assert TORCH_REQ.is_file(), f"{TORCH_REQ} is missing"
    assert "download.pytorch.org/whl/cpu" in TORCH_REQ.read_text(), (
        "requirements/torch-cpu.txt no longer installs torch from the CPU index-url. "
        "Every workflow that runs the pytest suite installs this file, and so does "
        "containers/tiled/Dockerfile, whose build asserts torch.cuda.is_available() is "
        "False -- dropping the index-url swaps in the ~2.5 GB CUDA wheel everywhere."
    )


def test_the_torch_and_kornia_files_still_pin_what_they_are_for():
    """The pins moved into files; assert the files still hold a pin.

    Without this the two checks above degrade into "a file was installed" and would pass
    over an EMPTY requirements file -- the vacuous-guard failure this repo keeps hitting.
    The exact versions are deliberately not written here: that would recreate the second
    copy the move exists to delete.
    """
    for path, package in ((TORCH_REQ, "torch"), (KORNIA_REQ, "kornia")):
        assert path.is_file(), f"{path} is missing"
        lines = [ln.split("#", 1)[0].strip() for ln in path.read_text().splitlines()]
        pins = [ln for ln in lines if re.match(rf"^{package}==\S", ln)]
        assert len(pins) == 1, (
            f"{path.relative_to(REPO)} must pin {package} exactly once with `==`; found "
            f"{pins}. containers/tiled/Dockerfile and every pytest workflow install this "
            f"file, so an unpinned or missing line silently changes both at once."
        )


def test_every_pytest_workflow_proves_the_disk_case_is_not_skipped():
    """The claim in this file's docstring, actually asserted.

    An install line proves a wheel was fetched. It does not prove the library
    imports -- and importorskip cannot tell the difference between "not installed"
    and "installed but unimportable". So each in-scope workflow must import both
    libraries in the interpreter pytest uses, run
    tests/test_coarse_frontend.py, and fail on any skip it reports.
    """
    missing = []
    for wf, name, job in jobs_running_the_pytest_suite():
        # Resolved through the job's composite actions: the proof steps live in
        # the workflow today, but reading only `wf.read_text()` is what made this
        # file's install checks go blind when the installs moved, and there is no
        # reason to leave the same trap set for the next move.
        text = ci_actions.job_run_scripts(job)
        where = f"{wf.relative_to(REPO)}: job `{name}`"
        if not _IMPORT_PROOF_RE.search(text):
            missing.append(
                f"{where}: no `python -c \"import torch, kornia\"` step -- an install "
                "line does not prove the wheel imports on this runner"
            )
        if not _NO_SKIP_PROOF_RE.search(text):
            missing.append(
                f"{where}: no step that runs tests/test_coarse_frontend.py and fails "
                "on a reported skip -- so nothing here asserts the DISK case ran"
            )
    assert not missing, (
        "job(s) run the pytest suite without proving the DISK front-end case "
        "actually executes:\n" + "\n".join(missing)
    )
