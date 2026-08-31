"""No reference to the deleted COARSE front-ends outside history and the allow-list.

Spec 2026-08-28 Phase 2b. Scope is `git ls-files` -- the TRACKED tree -- deliberately,
not Path.rglob: rglob ignores .gitignore and matched 107 untracked files (.nf-test work
dirs, virtualenvs, .planning/, docs/superpowers/) including this guard's own plan.
"""

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# NOT \b. `\b` treats `_` as a word character, so it MISSES exactly the identifiers
# this guard exists to keep out: `_frontend_orb`, `_orb_features`, `normalize_for_orb`,
# `_ORB_PCT`, `_sift_features` and `_frontend_fourier_mellin` all slipped through the
# original \b form -- verified against the compiled pattern, not reasoned about. The
# lookaround form below excludes only letters and digits, so `_` becomes a boundary.
#
# The pre-flight scan's counterexamples were re-run against THIS form: absorb, orbit,
# absorbance, sifting, shift and drift still produce no match, so the fix costs nothing.
PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(orb|sift|fourier[_ -]?mellin|reg_tiled_frontend)(?![A-Za-z0-9])",
    re.I,
)

EXCLUDE_PREFIXES = ("docs/_archive/", "tests/testdata/")
ALLOW_FILES = {
    # History. Keeps its mentions by design.
    "CHANGELOG.md",
    # reg_benchmark keeps skimage's ORB as an INDEPENDENT accuracy oracle -- a
    # measurement tool, not a registration front-end. See this task's scope note.
    "bin/utils/reg_benchmark.py",
    "tests/test_reg_benchmark.py",
    "tests/test_no_legacy_frontends.py",
    # The COMPANION guard. test_the_deleted_frontends_are_really_gone names all eight
    # deleted symbols (`_frontend_orb`, `normalize_for_orb`, ...) in a hasattr sweep --
    # naming them is the whole point of it. It was passing the ORIGINAL \b pattern only
    # because \b treats `_` as a word character; tightening the pattern surfaced it at
    # once. This guard covers stray TEXT, that one covers live ATTRIBUTES; the exemption
    # is what keeps them from cancelling each other out.
    "tests/test_coarse_frontend.py",
    # These quote the literal runtime error string "RuntimeError: ORB found no
    # features." that a real CI pin exists for. Rewording them would break the pin.
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
    "tests/test_ci_stack_pinned.py",
    # Design/research records. NOT merely "historical" -- both are PUBLISHED (mkdocs.yml
    # :18-19) and bin/tiled_coarse.py cites the design doc for the thumbnail rationale, so
    # an uncorrected memory model in it is a live, operator-facing claim rather than an
    # archived one.
    #
    # The stated reason here USED to be "its remaining ORB mentions are in that banner and
    # in prose describing what the method USED to be". That was false when written: the
    # design doc's §5 ASCII block ("COARSE thumbnail feature-align (ORB + RANSAC) ... ~1-2
    # GB"), its primitive-split sentence ("COARSE **uses** feature matching (ORB/RANSAC)")
    # and its §11 sparse-tissue note ("ORB on DAPI needs enough keypoints") were all in the
    # PRESENT tense about the live method. An allow-list entry whose stated reason has
    # counterexamples is the pattern CLAUDE.md names by hand, so those three were rewritten
    # to DISK/LightGlue rather than the reason being softened around them.
    #
    # The reason now holds. What remains in the design doc is: the "superseded in part"
    # banner, one past-tense OOM anecdote in §5 ("COARSE was in fact implemented with a
    # full-resolution ORB ... before it was made to match this design"), one parenthetical
    # in the §11 note recording what that note was written against, and the
    # "Implementation status" phase list, which records what landed on
    # `feat/tiled-registration` and names deleted files (`bin/tiled_register.py`,
    # `modules/local/tiled_register.nf`) alongside it. All four are records of the past,
    # which is exactly what an exemption is for. If a NEW present-tense mention appears,
    # fix the doc -- do not widen this reason again.
    #
    # The research doc is a genuine survey of prior art -- ORB and SIFT are the names of
    # the algorithms it surveys, and renaming them would be a lie.
    "docs/parallel_registration_design.md",
    "docs/parallel_registration_research.md",
    # A published supplementary figure. It is now factually wrong about the method, but
    # spec Phase 6 owns rewriting it -- half-rewriting it here would be worse than
    # leaving it to its owner.
    "docs/figures/registration-schematic.html",
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
    # NOT `assert keep`. With `len(out) > 100` asserted above and ALLOW_FILES holding a
    # dozen entries, `keep` cannot empty without the count tripping first -- that form was
    # decoration, not a second check. This one can actually fire: it catches a filter that
    # silently swallows most of the tree (a bad suffix set, an EXCLUDE_PREFIXES typo that
    # matches everything, an ALLOW_FILES that grew into a blanket), which is the realistic
    # way this guard would go quietly vacuous while still reporting zero hits.
    assert len(keep) > 0.8 * len(out), (
        f"the filter kept only {len(keep)} of {len(out)} tracked files -- it is excluding "
        "most of the repo, so a clean result would prove nothing"
    )
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
