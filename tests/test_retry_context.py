"""RetryContext is a for-loop iterator. Exhaustion must end the loop, not
escape it.

The bug this guards: __next__ raised self.last_exception on exhaustion, so
the `for attempt in ctx:` loop never completed normally -- the exception
propagated out of the loop, past the caller's post-loop partial-failure
handling, and killed the whole task. On a 400+ GB REGISTER run that meant
losing the job instead of dropping one slide, and it made
`all_attempts_failed` unreachable dead code (bin/register.py already checks
it at line ~885, but could never get there).

Note: `all_attempts_failed` is a `@property` on RetryContext, not a callable
method -- the architecture-review brief that spawned this test described it
as `all_attempts_failed()`, but the actual code (and its one existing caller
in bin/register.py) already uses it as a property. This test follows the
real code, not the brief.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = str(REPO_ROOT / "bin")
UTILS_DIR = str(REPO_ROOT / "bin" / "utils")
for _dir in (BIN_DIR, UTILS_DIR):
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

from retry import RetryContext  # noqa: E402


def test_exhaustion_ends_the_loop_rather_than_raising():
    ctx = RetryContext(max_attempts=3)
    attempts = []
    for attempt in ctx:
        attempts.append(attempt)
        ctx.failed(RuntimeError(f"boom {attempt}"))
    assert attempts == [1, 2, 3]


def test_all_attempts_failed_is_reachable_and_true_after_exhaustion():
    ctx = RetryContext(max_attempts=2)
    for attempt in ctx:
        ctx.failed(RuntimeError("boom"))
    assert ctx.all_attempts_failed is True


def test_success_stops_iteration_and_reports_not_failed():
    ctx = RetryContext(max_attempts=3)
    seen = []
    for attempt in ctx:
        seen.append(attempt)
        ctx.succeeded()
    assert seen == [1]
    assert ctx.all_attempts_failed is False


def test_last_exception_is_preserved_for_the_caller_to_re_raise():
    """The exception is not swallowed -- it is handed to the caller, whose job
    it is to decide between 'drop this slide' and 'fail the task'."""
    ctx = RetryContext(max_attempts=2)
    boom = RuntimeError("the real cause")
    for attempt in ctx:
        ctx.failed(boom)
    assert ctx.all_attempts_failed
    assert ctx.last_exception is boom
