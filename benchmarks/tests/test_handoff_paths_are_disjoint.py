"""The arms and sweep experiments must never write to the same directory.

They did. Both submit scripts printed `--outdir benchmarks/paper_data` as the
documented next step, so whichever analysis ran second silently overwrote the
first's measurements.csv -- the two experiments write the SAME nine filenames --
and pull_to_ihc_method.sh then scraped whatever was left into
`ihc_method/data/benchmark/`, where an R page renders it cleanly with the wrong
answer.

Commit 103e47c separated the hand-offs in the Makefile and is worded as though
that closed it. `git show --stat 103e47c` never touched submit_arms.sh or
submit_sweep.sh, and those are the CLUSTER path -- the one actually used. A
Makefile-only fix does not reach it.

The roots are `benchmarks/_handoff/sweep` and `benchmarks/_handoff/arms`, which
is what the Makefile's `$(HANDOFF)/sweep` and `$(HANDOFF)/arms` already
resolved to; the submit scripts are aligned to them rather than the other way
round.

`paper_data` is now absent from every tracked script, INCLUDING as a fallback.
pull_to_ihc_method.sh used to search it when `_handoff/sweep` was missing, for
"a tree built before this script grew --sweep" -- but a legacy `paper_data/`
could hold either experiment's tables, so that fallback is the collision's last
surviving arm. Failing loudly and naming the target that builds the tables is
strictly better than copying an ambiguous directory into the consumer.
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTDIR = re.compile(r"--outdir\s+(\S+)")


def _outdirs(rel):
    """Every `--outdir X` in a script, with shell quoting stripped.

    These live inside `echo "..."` lines, so the captured token carries the
    echo's closing quote. Leaving it on makes every comparison below a
    near-miss that reads like a real divergence.
    """
    return {
        v.strip("\"'").rstrip("/")
        for v in OUTDIR.findall((ROOT / rel).read_text())
    }


def test_arm_and_sweep_handoffs_are_disjoint():
    arms = _outdirs("benchmarks/submit_arms.sh")
    sweep = _outdirs("benchmarks/submit_sweep.sh")
    assert arms, "no --outdir found in submit_arms.sh -- the extractor is stale"
    assert sweep, "no --outdir found in submit_sweep.sh -- the extractor is stale"
    assert not (arms & sweep), (
        f"arms and sweep both write to {sorted(arms & sweep)} -- the second analysis "
        f"to run overwrites the first's measurements.csv, and the two experiments "
        f"write the same nine filenames"
    )


def _tracked_shell_files():
    listed = subprocess.run(
        ["git", "ls-files", "benchmarks/*.sh", "Makefile", "benchmarks/Makefile"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    return [r for r in listed if (ROOT / r).exists()]


def test_every_tracked_script_agrees_on_the_two_roots():
    """A third script reaching for the old shared path re-arms the collision,
    whether it writes there or merely READS there as a fallback."""
    offenders = []
    for rel in _tracked_shell_files():
        for i, line in enumerate((ROOT / rel).read_text().splitlines(), 1):
            # A whole-line shell comment is prose, not a reference. Three scripts
            # explain this defect's history in exactly those terms, and a check
            # that could not tell that apart would have to be deleted the first
            # time someone documented the bug they had just fixed. An INLINE
            # trailing comment is still scanned, because the code on that line is.
            if line.lstrip().startswith("#"):
                continue
            if re.search(r"paper_data(?![/\w])", line):
                offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, (
        "these still reference the shared benchmarks/paper_data root rather than "
        "benchmarks/_handoff/sweep or benchmarks/_handoff/arms:\n  "
        + "\n  ".join(offenders)
    )


def test_the_scan_reads_the_scripts_it_claims_to():
    """Both checks above are file-scans, and a `git ls-files` glob that matched
    nothing would make them pass while reading no script at all."""
    files = _tracked_shell_files()
    assert len(files) >= 4, f"only {len(files)} tracked script(s) scanned: {files}"
    for required in ("benchmarks/submit_arms.sh", "benchmarks/submit_sweep.sh"):
        assert required in files, f"{required} is not in the scanned set"


def test_the_makefile_and_the_submit_scripts_name_the_same_roots():
    """The Makefile and the cluster path are two ways to run the same two
    experiments. They diverged once already -- that IS this defect -- so the
    agreement is asserted rather than assumed.

    The Makefile parameterises the staging dir (`HANDOFF ?= benchmarks/_handoff`)
    and the submit scripts spell it out, so this compares the resolved strings.
    """
    makefile = (ROOT / "Makefile").read_text()
    m = re.search(r"^HANDOFF\s*\?=\s*(\S+)", makefile, re.M)
    assert m, "Makefile no longer defines HANDOFF"
    handoff = m.group(1)
    assert f"{handoff}/arms" in makefile.replace("$(HANDOFF)", handoff), (
        "the Makefile no longer stages arm tables under $(HANDOFF)/arms"
    )
    assert f"{handoff}/sweep" in makefile.replace("$(HANDOFF)", handoff), (
        "the Makefile no longer stages sweep tables under $(HANDOFF)/sweep"
    )
    assert any(o.rstrip("/").endswith(f"{handoff}/arms")
               for o in _outdirs("benchmarks/submit_arms.sh")), (
        f"submit_arms.sh does not name {handoff}/arms; it and the Makefile would "
        f"stage the same experiment in two different places"
    )
    assert any(o.rstrip("/").endswith(f"{handoff}/sweep")
               for o in _outdirs("benchmarks/submit_sweep.sh")), (
        f"submit_sweep.sh does not name {handoff}/sweep"
    )


def test_no_synthetic_run_sits_where_real_results_land():
    """An unrun benchmark must not look run.

    run0000/ and run0001/ are hand-written -- `-` timestamps, input sizes at
    exact powers of two, round-number durations -- and they used to sit at
    benchmarks/runs/, the directory the cluster writes real results into, as the
    ONLY thing there. A fresh checkout therefore read as a completed benchmark.
    Nothing has ever been run: no paper_data/, and analysis/figures/ holds only
    its own .gitignore.

    They belong under tests/fixtures/, where they are unmistakably test input.
    """
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", "benchmarks"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    misplaced = [
        p for p in tracked
        if p.startswith("benchmarks/runs/")
        or p.startswith("benchmarks/paper_data/")
        or p.startswith("benchmarks/_handoff/")
    ]
    assert not misplaced, (
        "these are committed under a RESULTS root, so a fresh checkout looks "
        f"like a benchmark that has been run: {misplaced}"
    )


def test_the_synthetic_fixtures_say_what_they_are():
    """The fixtures are still synthetic and still labelled. Without the label,
    the next person to open trace.txt has to infer it from the `-` timestamps."""
    fixtures = ROOT / "benchmarks" / "tests" / "fixtures" / "runs"
    assert fixtures.is_dir(), "the synthetic run fixtures are gone"
    readme = fixtures / "README.md"
    assert readme.exists(), "benchmarks/tests/fixtures/runs/README.md is missing"
    assert "NOT DATA" in readme.read_text()
    # And they are still recognisably synthetic, so the README is not describing
    # something that has quietly been replaced with real output.
    trace = (fixtures / "run0000" / "trace" / "trace.txt").read_text()
    assert "\t-\t" in trace, (
        "run0000's trace no longer carries the placeholder `-` timestamps that "
        "make it obviously hand-written"
    )
