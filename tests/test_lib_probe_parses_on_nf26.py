"""tests/lib_probe.nf must COMPILE on Nextflow 26, not just 25.

lib_probe.nf is the only unit-test surface lib/*.groovy has -- nf-test's
assertion context cannot see lib/ classes (tests/layout.nf.test:5 records this),
and the repo has no JVM test runner. CI's `nextflow-stub` job runs it in BOTH
matrix legs, `25.04.0` and `latest-everything`.

It did not compile on the second one. Two constructs Nextflow 26's strict parser
rejects had accumulated, and the failure mode is the worst available: the script
fails to COMPILE, so every assertion in it is skipped, and the only signal is one
red step in one matrix leg.

  1. `assert cond, 'message'`  -- the old Groovy comma form.
     NF26: "Unexpected input: ','". The documented form is
     `assert cond : 'message'`, which parses on both. 31 sites.

  2. `someClosure(arg)` where `someClosure` is a `def`-bound closure.
     NF26: "`keysOf` is not defined". It must be `someClosure.call(arg)`.
     workflows/mirage.nf already carries a comment recording this same
     restriction ("the strict Nextflow parser cannot invoke a closure-typed
     local as a function"); the probe had not learned it.

Verified after the fix, both directions on both engines: unmodified exits 0 on
25.04.7 and 26.04.6, and with one assertion deliberately falsified it exits 1 on
both -- so the assertions genuinely run rather than merely compiling.

This guard is a cheap static stand-in for that. A full compile check would mean
launching Nextflow twice from pytest, which the Python CI job has no engine for;
CI's own two-leg matrix is the real check, and this is what fails fast in the
suite the author runs first.
"""
import re
from pathlib import Path

from tests.nfmodel import strip_comments

PROBE = Path(__file__).resolve().parents[1] / "tests" / "lib_probe.nf"
SRC = strip_comments(PROBE.read_text())

# `assert <anything>, <quoted message>` at end of line, or with the message on
# the following line. Both are the comma form.
_COMMA_ASSERT = re.compile(
    r"^\s*assert\s+.*,\s*(?:'[^']*'|\"[^\"]*\")\s*$", re.M
)
_COMMA_ASSERT_CONTINUED = re.compile(
    r"^\s*assert\s+[^\n]*,\s*\n\s*(?:'[^']*'|\"[^\"]*\")\s*$", re.M
)


def test_no_assert_uses_the_comma_message_form():
    offenders = [
        m.group(0).strip()
        for pat in (_COMMA_ASSERT, _COMMA_ASSERT_CONTINUED)
        for m in pat.finditer(SRC)
    ]
    assert not offenders, (
        "Nextflow 26's parser rejects `assert cond, 'msg'` with "
        "\"Unexpected input: ','\" and the whole script fails to compile, "
        "skipping every assertion in it. Use `assert cond : 'msg'`:\n  "
        + "\n  ".join(offenders)
    )


def test_no_def_bound_closure_is_invoked_as_a_function():
    """`def f = { ... }` then `f(x)` compiles on 25 and does not on 26."""
    closures = set(re.findall(r"^\s*def\s+(\w+)\s*=\s*\{", SRC, re.M))
    offenders = []
    for name in sorted(closures):
        for m in re.finditer(rf"(?<![.\w]){re.escape(name)}\s*\(", SRC):
            offenders.append(
                f"`{name}(...)` at offset {m.start()} -- a def-bound closure "
                f"invoked as a function; NF26 reports \"`{name}` is not "
                f"defined\". Use {name}.call(...)"
            )
    assert not offenders, "\n".join(offenders)


def test_the_scan_sees_the_probe():
    """If lib_probe.nf were renamed or emptied, both checks above would pass
    while covering nothing -- and the file they protect is the only unit-test
    surface lib/*.groovy has."""
    assert PROBE.exists(), "tests/lib_probe.nf is gone"
    n_asserts = len(re.findall(r"^\s*assert\s", SRC, re.M))
    assert n_asserts >= 100, (
        f"only {n_asserts} assertion(s) in lib_probe.nf -- it has been gutted, "
        "not merely reformatted"
    )
    assert re.search(r"^\s*def\s+\w+\s*=\s*\{", SRC, re.M), (
        "lib_probe.nf no longer binds any closure, so the second check above "
        "has nothing to walk"
    )
