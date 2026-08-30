"""No reference to the deleted COARSE front-ends outside history and the allow-list.

Spec 2026-08-28 Phase 2b. Scope is `git ls-files` -- the TRACKED tree -- deliberately,
not Path.rglob: rglob ignores .gitignore and matched 107 untracked files (.nf-test work
dirs, virtualenvs, .planning/, docs/superpowers/) including this guard's own plan.
"""

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PATTERN = re.compile(r"\b(orb|sift|fourier[_ -]?mellin|reg_tiled_frontend)\b", re.I)
# Verified by the pre-flight scan: \b is correct here -- absorb, orbit, absorbance,
# sifting, shift and drift all produce NO match. The word-boundary worry was unfounded.

EXCLUDE_PREFIXES = ("docs/_archive/", "tests/testdata/")
ALLOW_FILES = {
    # History. Keeps its mentions by design.
    "CHANGELOG.md",
    # reg_benchmark keeps skimage's ORB as an INDEPENDENT accuracy oracle -- a
    # measurement tool, not a registration front-end. See this task's scope note.
    "bin/utils/reg_benchmark.py",
    "tests/test_reg_benchmark.py",
    "tests/test_no_legacy_frontends.py",
    # These quote the literal runtime error string "RuntimeError: ORB found no
    # features." that a real CI pin exists for. Rewording them would break the pin.
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
    "tests/test_ci_stack_pinned.py",
    # Historical design/research records, kept per Task 4's scope note. Listing them
    # here is what makes that ruling explicit instead of a silent contradiction.
    "docs/parallel_registration_design.md",
    "docs/parallel_registration_research.md",
    # A published supplementary figure. It is now factually wrong about the method, but
    # spec Phase 6 owns rewriting it -- half-rewriting it here would be worse than
    # leaving it to its owner.
    "docs/figures/registration-schematic.html",
    # TEMPORARY -- Task 4 deletes both of these files outright (it removes the `ashlar`
    # backend). When it does, DELETE these two entries too; leaving them behind would
    # silently re-open the hole this guard exists to close.
    "subworkflows/local/adapters/ashlar_adapter.nf",
    "tests/test_ashlar_solve.py",
}


def _candidates():
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split()
    assert len(out) > 100, f"git ls-files returned only {len(out)} paths -- scope is wrong"
    keep = [
        r
        for r in out
        if r not in ALLOW_FILES
        and not r.startswith(EXCLUDE_PREFIXES)
        and Path(r).suffix not in {".pyc", ".png", ".tif", ".tiff"}
    ]
    assert keep, "every tracked file was excluded -- the filter is wrong, not the repo"
    return keep


def test_no_legacy_frontend_references():
    hits = []
    for rel in _candidates():
        p = REPO / rel
        if not p.is_file():
            continue
        for i, line in enumerate(p.read_text(errors="ignore").splitlines(), 1):
            if PATTERN.search(line):
                hits.append(f"{rel}:{i}: {line.strip()[:120]}")
    assert not hits, (
        "reference(s) to a deleted COARSE front-end remain:\n" + "\n".join(hits)
    )
