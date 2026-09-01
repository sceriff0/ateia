#!/usr/bin/env python3
"""Guard: nothing in this repository ends a command with `|| true`.

THE REGISTER, AND WHY IT IS A TEST RATHER THAN A LIST IN A DOCUMENT. The CI redesign
(2026-09-01) enumerated every `|| true` in the tree and gave each a disposition. An
enumeration written once is a snapshot; the next `|| true` is added by someone who
never read it. So the register is enforced here instead, and the file it is enforced
over is discovered rather than listed.

`|| true` is the *visible* way a check passes while proving nothing:
`nf-core lint --dir . || true` ran in CI for months over a command that could not
lint this repository at all (see .nf-core.yml's header for the version-by-version
measurement), and nobody could tell, because the job was green either way.

WHAT WAS DONE WITH THE FIVE THAT EXISTED, so this file reads as a record and not as
a rule with no history:

  * `ci.yml`'s `nf-core lint --dir . || true`  -- the JOB was deleted (Phase 7).
  * `check_profiles_parse.sh`'s
    `[ "$moved" = 1 ] && mv ... || true`       -- rewritten to an `if`. That is the
    classic `A && B || C` trap: the `|| true` was there to stop the `&&` returning 1
    when the file was never moved, and it swallowed a FAILED `mv` at the same time.
  * `resume_check.sh`'s `cached=$(grep -c ... || true)`  -- rewritten `|| cached=0`.
  * `_test-suite.yml`'s `n=$(... | grep -ci ... || true)` -- rewritten `|| n=0`.
    Both of those are the "nonzero means ZERO MATCHES" idiom with the real assertion
    on the next line; nothing was being swallowed, and writing them as the
    assignments they are means no reader has to classify a `|| true` again.
  * `.readthedocs.yaml`'s `git fetch --unshallow || true`  -- rewritten to ask git
    whether the clone is shallow. This one was on NO version of the register: it was
    found by re-running the scan against the tree instead of trusting the list, which
    is the entire reason this file exists.

SCOPE. Every `run:` body in `.github/workflows/**` and `.github/actions/**`
(resolved through `tests/ci_actions.py`, never a private parse), every tracked shell
script, `.readthedocs.yaml`, and every Nextflow process `script:`/`stub:` body (via
`tests/nfmodel`, never a private regex).

COMMENT-BLIND, in the direction that matters. Comments here can only make the scan
report MORE, never less -- a `#` line explaining a removed `|| true` necessarily
quotes it -- so they are stripped, including trailing ones, and the docstring above
is why: this file's own prose would otherwise fail it.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))
ci_actions = importlib.import_module("ci_actions")

from tests.nfmodel import processes, strip_comments  # noqa: E402

NEEDLE = "|| true"

# The ONE remaining occurrence, with its reason. BY (path, the exact command), so an
# exemption cannot silently cover a second one added to the same file later.
#
# Keep this dict as close to empty as the tree allows. An entry is a debt, not a
# licence: it has to name the command, and test_the_allowlist_has_no_dead_entries
# below deletes it the moment the command changes.
ALLOWED: dict[tuple[str, str], str] = {
    (
        "modules/local/register.nf",
        'find -L ref input_* -maxdepth 1 -type f \\\\( -name "*.ome.tif" -o -name '
        '"*.ome.tiff" \\\\) 2>/dev/null > files_to_copy.txt || true',
    ): (
        "The same 'nonzero means zero matches' idiom as the two rewritten above: "
        "`find` exits 1 when a named path does not exist, and a REGISTER task whose "
        "`input_*` glob matched nothing is a case the script handles rather than an "
        "error. The real, named assertion is twenty lines below in the same block -- "
        "`if [ \"$file_count\" -eq 0 ]; then ... exit 1; fi` -- so nothing is "
        "swallowed; it is simply not on the NEXT line, which is why this is an entry "
        "here instead of a rewrite.\n"
        "It was left alone deliberately, and the cost of changing it is the reason: "
        "this is inside a process `script:` block, so any edit changes the rendered "
        "command, changes the task hash, and invalidates the cached REGISTER task in "
        "every existing work directory -- the most expensive process in the pipeline "
        "(measured at 483 GB and hours per task). A CI-hygiene phase is not where "
        "that gets spent. If REGISTER's script is being rewritten for another reason, "
        "fold the `if` in then and delete this entry."
    ),
}

# Non-vacuity floors. A "must not find" scan over an empty set is the failure this
# whole redesign is about, and every one of these scopes has a plausible way to go
# quietly empty: a resolver that stops resolving, a `git ls-files` filter that
# matches nothing, a model that stops finding processes.
MIN_CI_COMMAND_LINES = 100
MIN_SHELL_SCRIPTS = 3
MIN_PROCESSES = 15


def _shell_scripts() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.sh"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split()
    return [REPO / rel for rel in out]


def _clean(text: str) -> str:
    """Commands only: whole-line and trailing shell comments removed."""
    return ci_actions.strip_shell_comments(text)


def _hits(where: str, text: str) -> list[tuple[str, str]]:
    return [
        (where, line.strip())
        for line in text.splitlines()
        if NEEDLE in line
    ]


def _all_hits() -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []

    for path in ci_actions.scanned_files():
        rel = path.relative_to(REPO).as_posix()
        hits += _hits(rel, _clean(ci_actions.run_scripts(path)))

    for path in _shell_scripts():
        hits += _hits(path.relative_to(REPO).as_posix(), _clean(path.read_text()))

    rtd = REPO / ".readthedocs.yaml"
    if rtd.is_file():
        hits += _hits(".readthedocs.yaml", _clean(rtd.read_text()))

    for proc in processes().values():
        rel = proc.path.relative_to(REPO).as_posix()
        body = strip_comments(proc.script_body) + "\n" + strip_comments(proc.stub_body)
        hits += _hits(rel, body)

    # Deduplicate: `run_scripts` resolves a composite action into every workflow that
    # uses it, so one line in an action would otherwise be reported many times.
    return sorted(set(hits))


def test_the_scan_actually_read_something():
    """Every floor below has a way of going quietly to zero, and a zero-answer scan
    is exactly what this file is written against."""
    ci_text = "\n".join(
        ci_actions.run_scripts(path) for path in ci_actions.scanned_files()
    )
    ci_lines = [line for line in ci_text.splitlines() if line.strip()]
    assert len(ci_lines) >= MIN_CI_COMMAND_LINES, (
        f"only {len(ci_lines)} command line(s) resolved out of "
        f"{[p.name for p in ci_actions.scanned_files()]}; the workflows and actions "
        "run far more than that, so ci_actions stopped resolving them."
    )
    assert len(_shell_scripts()) >= MIN_SHELL_SCRIPTS, (
        f"git ls-files '*.sh' returned {len(_shell_scripts())} script(s); this repo "
        "tracks several under tests/, so the scope glob is wrong."
    )
    assert len(processes()) >= MIN_PROCESSES, (
        f"tests.nfmodel found {len(processes())} process(es); modules/local/ holds far "
        "more, so the model read nothing."
    )


def test_the_scan_is_comment_blind():
    """Proved rather than claimed, because the opposite has happened here twice.

    A trailing comment on a real command line is the shape that got past a guard on
    2026-09-01: `true  # bash tests/cleanup_work.sh` still matched a whole-file text
    search. Both halves are checked here on synthetic input, so this file cannot
    quietly regain either blind spot.
    """
    assert NEEDLE not in _clean("# nf-core lint --dir . || true")
    assert NEEDLE not in _clean("echo hello  # this used to be `foo || true`")
    # ...and a REAL one is still seen, including after a string containing a `#`.
    assert NEEDLE in _clean("foo || true")
    assert NEEDLE in _clean("""echo "a # b" && foo || true""")


def test_nothing_swallows_a_failure_with_or_true():
    offenders = [
        f"{where}: {line}"
        for where, line in _all_hits()
        if (where, line) not in ALLOWED
    ]
    assert not offenders, (
        "`|| true` makes a command that failed report success, which is the visible "
        "way a check passes while proving nothing -- `nf-core lint --dir . || true` "
        "ran for months over a command that could not lint this repository at all.\n\n"
        "If the exit status genuinely carries data rather than failure (`grep -c` "
        "exits 1 to mean ZERO MATCHES), write the assignment: `n=$(...) || n=0`, with "
        "the real assertion next to it. If the command may legitimately be a no-op, "
        "ask the question directly (`git rev-parse --is-shallow-repository`) instead "
        "of discarding the answer. Only if neither is possible, add it to ALLOWED "
        "with the command and a reason.\n\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize(
    "entry", sorted(ALLOWED), ids=[f"{w}:{c[:40]}" for w, c in sorted(ALLOWED)]
)
def test_the_allowlist_has_no_dead_entries(entry):
    """An exemption that exempts nothing is a licence nobody is using and the next
    reader will trust. This repository has had three of those in one file at once."""
    assert entry in _all_hits(), (
        f"ALLOWED entry {entry} matches no `|| true` in the tree any more. Either the "
        "command was rewritten -- in which case delete the entry in the same commit -- "
        "or it moved and the entry now exempts nothing while reading as though it does."
    )
