"""The DISK front-end test must actually RUN in CI, not skip.

tests/test_coarse_frontend.py guards its DISK+LightGlue cases with
importorskip("torch")/importorskip("kornia"). Before this task CI had no torch, so
those cases skipped silently -- and a silent skip is how an unimplemented front-end
shipped in the first place.

SCOPE IS PER-FILE, DELIBERATELY. An earlier version of this guard concatenated
*every* file under `.github/workflows/` and asserted against the union. That is a
mask, not a scope: deleting BOTH torch and kornia installs from
`.github/workflows/ci.yml` -- the workflow whose `python-tests` job is CI's blocking
gate -- left `release.yml`'s copy standing, and every assertion here passed (10
passed). In that state the DISK cases importorskip away in the gate that actually
runs on every push, which is precisely the regression this file exists to catch. Each
workflow that runs the pytest suite is therefore checked ON ITS OWN.

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

Plain pytest, no pipeline-runtime imports, all paths derived from `__file__`, per
this repo's other guard tests.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
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
    files = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    assert files, f"no workflow files under {WORKFLOWS}"
    return files


def workflows_running_the_pytest_suite():
    """Every workflow that runs the Python unit-test suite, one entry per FILE.

    Discovered by looking for a `pytest ... tests/` invocation, never by filename.
    """
    hits = [wf for wf in workflow_files() if _RUNS_THE_SUITE_RE.search(wf.read_text())]
    # Non-vacuity: an empty list would make every parametrised check below pass
    # having examined nothing, which is the failure mode this whole file is about.
    assert hits, (
        f"no workflow under {WORKFLOWS.relative_to(REPO)} runs `pytest ... tests/`. "
        "Either the suite stopped running in CI, or the invocation was reworded past "
        "this detector -- update _RUNS_THE_SUITE_RE rather than leaving the checks "
        "below to pass over an empty set."
    )
    return hits


def test_every_pytest_workflow_installs_torch_and_kornia():
    """Per-file, not a union. See this module's docstring for the measured break."""
    missing = []
    for wf in workflows_running_the_pytest_suite():
        text = wf.read_text()
        rel = wf.relative_to(REPO)
        if not _TORCH_INSTALL_RE.search(text):
            missing.append(f"{rel}: no `pip install -r requirements/torch-cpu.txt`")
        if not _KORNIA_INSTALL_RE.search(text):
            missing.append(f"{rel}: no `pip install -r requirements/kornia.txt`")
    assert not missing, (
        "workflow(s) run the pytest suite without installing the DISK front-end "
        "stack, so tests/test_coarse_frontend.py's DISK cases importorskip away "
        "there and prove nothing:\n" + "\n".join(missing)
    )


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
    for wf in workflows_running_the_pytest_suite():
        text = wf.read_text()
        rel = wf.relative_to(REPO)
        if not _IMPORT_PROOF_RE.search(text):
            missing.append(
                f"{rel}: no `python -c \"import torch, kornia\"` step -- an install "
                "line does not prove the wheel imports on this runner"
            )
        if not _NO_SKIP_PROOF_RE.search(text):
            missing.append(
                f"{rel}: no step that runs tests/test_coarse_frontend.py and fails "
                "on a reported skip -- so nothing here asserts the DISK case ran"
            )
    assert not missing, (
        "workflow(s) run the pytest suite without proving the DISK front-end case "
        "actually executes:\n" + "\n".join(missing)
    )
