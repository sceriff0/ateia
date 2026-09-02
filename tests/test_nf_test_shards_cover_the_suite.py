#!/usr/bin/env python3
"""Guard: the nf-test (stub) matrix's shard list and its `--shard` divisor agree.

Added 2026-09-02 (release 1.0, phase 02, task 3b) when `_test-suite.yml`'s
`nf-test-stub` job was sharded four ways. Measured on CI run 33641966166:
every other job in that file finishes in <= 5 min while `nf-test (stub)` alone
ran > 25 min over 214 tests, serially -- it was the whole CI wall-clock. nf-test
0.9.0+ (the pinned `NFTEST_VERSION`) supports `--shard i/n --shard-strategy
round-robin`, so the job became a 4-leg matrix instead.

WHAT CAN GO WRONG SILENTLY. `strategy.matrix.shard: [1, 2, 3, 4]` and the run
step's `--shard ${{ matrix.shard }}/4` are two independent places that both
encode "there are 4 shards", and nothing forces them to agree. `shard: [1, 2,
3]` with a divisor still reading `/4` would run only 3 of 4 quarters --
`round-robin` guarantees every fourth test lands on shard 4, so that quarter of
the suite would run on NO leg at all, and every leg that DID run would still be
green, because each shard only ever asserts on its own subset. That is exactly
this repository's "verification reality" failure shape (CLAUDE.md item 7,
generalised): nothing here is a comment, so `strip_line_comment` is not the
gap -- the gap is two numbers that were typed twice and can drift apart with no
test noticing either one.

This guard reads the shard divisor out of the job's `run:` bodies via
`ci_actions.job_run_scripts`, which strips whole-line AND trailing shell
comments (quote-aware) before matching -- so a `--shard` occurrence left behind
only inside a comment (e.g. commenting out the flag but leaving `# --shard
${{ matrix.shard }}/4` as an explanation) does not satisfy this test. See
CLAUDE.md, "Verification reality" item 7, and `tests/ci_actions.py`'s own
docstring for why a raw-text scan is the wrong view for a "does CI run this"
question.

Plain pytest, no pipeline-runtime imports, paths derived from `__file__` per
this repo's other guard tests (`test_ci_job_hygiene.py`, `test_ci_stack_pinned.py`).
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))
ci_actions = importlib.import_module("ci_actions")

SUITE = ci_actions.WORKFLOWS_DIR / "_test-suite.yml"
JOB_ID = "nf-test-stub"

# `--shard 2/4`, `--shard ${{ matrix.shard }}/4`, ... -- the divisor is whatever
# follows the NEAREST `/` after `--shard `. Not `\S*?`: a GitHub Actions
# expression (`${{ matrix.shard }}`) contains internal spaces, so the divisor
# does not always sit in the same whitespace-delimited token as `--shard`
# itself. `.*?` (non-greedy) stops at the first `/`, which is exactly the one
# closing the expression, and is anchored on `--shard` followed by whitespace
# so it does not also match `--shard-strategy` (a different flag with no
# `/n` of its own).
SHARD_RE = re.compile(r"--shard\s+.*?/(\d+)")


def _nf_test_stub_job() -> dict:
    jobs = ci_actions.load_jobs(SUITE)
    assert JOB_ID in jobs, (
        f"{SUITE.name} no longer has a `{JOB_ID}` job -- this guard, and "
        "`suite-gate`'s `needs:`, both key off that id staying stable across a "
        "matrix rewrite."
    )
    return jobs[JOB_ID]


def test_shard_matrix_list_is_contiguous_from_one():
    """`strategy.matrix.shard` must be exactly `[1, 2, ..., n]` for its length n.

    A gap (`[1, 2, 4]`) or a duplicate (`[1, 1, 2, 3]`) either strands a shard
    number nf-test never sees or runs one leg's slice twice while starving
    another -- and every individual leg still reports green, because a matrix
    leg only asserts on its own run.
    """
    job = _nf_test_stub_job()
    matrix = (job.get("strategy") or {}).get("matrix") or {}
    shard_list = matrix.get("shard")
    assert isinstance(shard_list, list) and shard_list, (
        f"{JOB_ID} has no non-empty `strategy.matrix.shard` list"
    )
    n = len(shard_list)
    assert shard_list == list(range(1, n + 1)), (
        f"{JOB_ID}'s matrix.shard is {shard_list!r}, expected the contiguous "
        f"list [1..{n}]. A gap or duplicate here silently drops or double-runs "
        "a quarter of the suite with no test failing."
    )


def test_shard_divisor_matches_the_matrix_length():
    """Every `--shard i/n` invocation in the job must use n == len(matrix.shard).

    Read through `ci_actions.job_run_scripts`, which strips shell comments
    (whole-line and trailing, quote-aware) before matching -- the same
    comment-blind view every "does CI run X" guard in this repo must use
    (CLAUDE.md, "Verification reality" item 7).
    """
    job = _nf_test_stub_job()
    matrix = (job.get("strategy") or {}).get("matrix") or {}
    shard_list = matrix.get("shard") or []
    n = len(shard_list)
    assert n, f"{JOB_ID} has no `strategy.matrix.shard` list to compare against"

    script = ci_actions.job_run_scripts(job)
    divisors = {int(d) for d in SHARD_RE.findall(script)}
    assert divisors, (
        f"no `--shard i/n` invocation found in {JOB_ID}'s `run:` bodies "
        "(comments stripped) -- the sharded nf-test command may have been "
        "removed or commented out."
    )
    assert divisors == {n}, (
        f"{JOB_ID}'s `--shard` divisor(s) {sorted(divisors)} do not match its "
        f"matrix length ({n} legs declared in `strategy.matrix.shard`). "
        f"matrix.shard and the `--shard i/n` divisor must be typed as the same "
        "number: a mismatch either strands tests (divisor > matrix length) or "
        "runs some of them on every leg while others run on none (divisor < "
        "matrix length)."
    )
