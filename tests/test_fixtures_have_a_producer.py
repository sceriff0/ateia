"""Every fixture a test opens must actually be there.

tests/testdata/ is gitignored (.gitignore:140,142) with a small tracked
exception, so a fixture is available at all only if
tests/testdata/generate_complete_testdata.py writes it or git carries it. A
fixture that is neither exists on exactly one developer's machine, and the test
that opens it fails everywhere else -- or passes locally and hides a regression
CI cannot see.

Not hypothetical. tests/testdata/seg_eval_P001.json and _P002.json were
referenced by tests/modules/merge_seg_eval.nf.test with `checkIfExists: true`
and produced by nothing: the CSE removal in cea0b47 took the writer away and
left the reader. They were 2 of the 3 nf-test failures standing on this branch,
and had been failing since that commit.

RULING R7, INVERTED. R7 says fixtures go in the generator and are never
committed. This is the other direction -- a fixture the generator never writes
-- and it is the one that actually bit.

WHY THIS CHECKS EXISTENCE RATHER THAN THE GENERATOR'S SOURCE. The first version
of this file grepped the generator for each filename, and it was wrong twice
over: it reported sample_DAPI_intensity.csv as an orphan (the generator writes
it through an f-string, so the literal name never appears) and it would have
missed a producer that had been deleted while its `open(...)` line survived.
Existence is the property that matters and the only one with no blind spot.

It binds in CI, which is where it must. Every job that touches fixtures runs the
generator into a fresh checkout first, so "the file is there" can only mean the
generator wrote it or git carries it. Locally it is weaker -- a stale file from
an earlier branch satisfies it -- and that is an acceptable asymmetry: the
failure being prevented is "green here, red in CI", and CI is where the strong
form runs.
"""
import re
import subprocess
from pathlib import Path

from tests.nfmodel import strip_comments

ROOT = Path(__file__).resolve().parents[1]
TESTDATA = ROOT / "tests" / "testdata"
GENERATOR = TESTDATA / "generate_complete_testdata.py"

# Any tests/testdata/<name> reference in an nf-test file, a Python test or a
# test config. A trailing '.' is prose punctuation, not part of the name.
_REF = re.compile(r"tests/testdata/([A-Za-z0-9_\-][A-Za-z0-9_.\-]*[A-Za-z0-9_\-])")


def _referenced():
    """fixture path (relative to tests/testdata) -> files that reference it."""
    refs = {}
    for path in sorted(ROOT.glob("tests/**/*")):
        if path.is_dir() or path.suffix not in (".test", ".py", ".nf", ".config"):
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        if TESTDATA in path.parents:
            continue  # the generator naturally names everything it writes
        # Comments blanked, STRINGS INTACT. A fixture named in a `//` comment --
        # this repo's tests explain their fixtures constantly -- is not a
        # reference, and would make this file demand a producer for a path
        # nothing opens. The path itself lives inside a quoted `file('...')`
        # argument, so the strings must survive or the scan finds nothing.
        for name in _REF.findall(strip_comments(path.read_text(errors="ignore"))):
            refs.setdefault(name, set()).add(str(path.relative_to(ROOT)))
    return refs


def test_every_referenced_fixture_exists():
    missing = [
        f"tests/testdata/{name} -- referenced by {', '.join(sorted(users))}"
        for name, users in sorted(_referenced().items())
        if not (TESTDATA / name).exists()
    ]
    assert not missing, "\n".join(missing) + (
        "\n\nRun `python tests/testdata/generate_complete_testdata.py`. If it "
        "still does not appear, the fixture has no producer: add it to that "
        "script (RULING R7 -- fixtures are generated, never committed, because "
        "tests/testdata/ is gitignored and a hand-written one is absent in CI)."
    )


def test_the_scan_finds_real_references():
    """A regex that matched nothing would pass the check above vacuously, and
    the failure it prevents is invisible from the interface."""
    refs = _referenced()
    assert len(refs) >= 15, (
        f"only {len(refs)} fixture reference(s) found across tests/ -- the scan "
        "is stale, not the tests"
    )
    assert GENERATOR.exists(), "the fixture generator is gone"


def test_the_generator_is_what_produces_the_untracked_ones():
    """The check above is satisfied by any file on disk. This pins the claim the
    docstring makes about WHERE those files come from: most referenced fixtures
    are NOT tracked by git, so something else must be writing them, and the
    generator is the only candidate."""
    tracked = {
        Path(p).name
        for p in subprocess.run(
            ["git", "ls-files", "tests/testdata"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.split()
    }
    referenced = set(_referenced())
    untracked = {n for n in referenced if Path(n).name not in tracked}
    assert len(untracked) > len(referenced) / 2, (
        "most referenced fixtures are now git-tracked, which contradicts RULING "
        "R7 and means tests/testdata/ has quietly stopped being generated: "
        f"{len(untracked)} of {len(referenced)} are untracked"
    )
