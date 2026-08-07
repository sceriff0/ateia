#!/usr/bin/env python3
"""Guard: no module hand-writes the versions.yml envelope.

Every process in modules/local/ used to write the heredoc twice — once in script:, once
in stub: with values replaced by `stub`. Because `-stub` never evaluates a script: block,
a tool present in one and absent from the other was invisible to the entire blocking CI
gate. lib/ProcessEnvelope.groovy renders both from one list, so this file's job is to keep
anyone from re-introducing a hand-written copy.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULES = sorted((ROOT / "modules" / "local").glob("*.nf"))

# Documented exceptions -- not oversights:
#
# segment.nf renders its version ROWS from lib/SegBackends.groovy's `versions` map, and
# those entries are pre-rendered YAML lines (e.g. a full
# 'deepcell: $(python -c "import deepcell; ...")' string), not plain python module
# names. ProcessEnvelope.versions()/versionsStub() only know how to turn a bare module
# name into a probe; routing segment.nf through them would mean teaching SegBackends to
# speak ProcessEnvelope's vocabulary instead, which is a rewrite of a class that predates
# this task and belongs to Task 3's neighbour, not this one.
#
# THIS IS NOT A HYPOTHETICAL RISK -- segment.nf is, RIGHT NOW, the single clearest live
# instance of the exact divergence this whole task exists to eliminate: its script:
# block reports `python` plus the chosen backend's rows (e.g. `deepcell`/`tensorflow`
# for StarDist), while its stub: block reports `python` plus a bare `seg_method: <name>`
# -- two DISJOINT key sets, hand-maintained, that -stub can never catch drifting apart.
# It is correctly out of scope for this task (see above), not "merely legacy" -- fixing
# it means SegBackends learning ProcessEnvelope's vocabulary, which is real work for
# whichever task inherits SegBackends next.
#
# aggregate_size_logs.nf runs in `container 'ubuntu:22.04'` and reports a `bash:` version
# row, not a `python:` one -- it has no Python interpreter at all. ProcessEnvelope always
# prepends a `python:` row (25 of 28 modules had one; this is the genuine anomaly), so
# routing this module through it would add a fabricated/`unknown` python entry to a
# published report that has never carried one.
ALLOWED_HANDWRITTEN = {"segment.nf", "aggregate_size_logs.nf"}


def test_no_module_hand_writes_a_versions_heredoc():
    offenders = []
    for nf in MODULES:
        if nf.name in ALLOWED_HANDWRITTEN:
            continue
        text = nf.read_text()
        if "END_VERSIONS" in text and "ProcessEnvelope." not in text:
            offenders.append(nf.name)
    assert not offenders, (
        f"{offenders} hand-write a versions.yml heredoc. Use "
        "ProcessEnvelope.versions()/versionsStub() so the script: and stub: copies "
        "cannot name different tools."
    )


CALL_START_RE = re.compile(r"ProcessEnvelope\.(versions|versionsStub)\(")

# A literal tools list: `task.process, ['a', 'b']` or `task.process, []`. Deliberately
# strict -- see _extract_calls' docstring for why a call that ISN'T this shape must fail
# loudly rather than be silently treated as "no call here".
LITERAL_ARGS_RE = re.compile(
    r"^\s*task\.process\s*,\s*(\[\s*(?:'[^']*'\s*(?:,\s*'[^']*'\s*)*)?\])\s*$"
)


def _find_balanced_call_args(text, open_paren_idx):
    """Given the index of the `(` that opens a call, return the substring of its
    arguments, respecting nested parens/brackets. Returns None if unbalanced."""
    depth = 0
    for i in range(open_paren_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_paren_idx + 1 : i]
    return None


def _extract_calls(text):
    """Every `ProcessEnvelope.versions(...)` / `ProcessEnvelope.versionsStub(...)` call
    site in `text`, as (method, args_string) pairs.

    Deliberately does NOT filter to only calls matching the literal-list shape: an
    earlier version of this guard's regex only matched `task.process, [...]` and simply
    found nothing (script_calls == stub_calls == []) for any other shape -- e.g. a
    variable holding the list, or (worse) a ternary picking between two lists per
    params.registration_method, which Task 3 introduces for warp_seg_qc.nf. A ternary
    matches the trailing literal branch only, producing a false "they match" instead of
    a skip. Either way the guard that exists specifically to catch a script:/stub:
    mismatch would pass while blind. So this function returns EVERY call site, parsed or
    not, and the caller is responsible for failing loudly on the ones it can't parse.
    """
    calls = []
    for m in CALL_START_RE.finditer(text):
        method = m.group(1)
        args = _find_balanced_call_args(text, m.end() - 1)
        calls.append((method, args))
    return calls


def _norm(text):
    return re.sub(r"\s+", "", text)


def test_versions_calls_are_literal_or_symmetric_between_script_and_stub():
    """Every ProcessEnvelope.versions()/versionsStub() call must be EITHER a literal
    `task.process, ['a', 'b']` tool list, OR a non-literal expression (a variable, a
    method call, a ternary) that is textually IDENTICAL -- modulo whitespace -- to its
    counterpart in the same module.

    Why a non-literal expression is allowed at all, when the docstring of
    _extract_calls above warns a ternary can fool this guard: the risk a ternary poses
    is picking a DIFFERENT branch in script: than in stub: (or matching the trailing
    literal branch only, in the regex-based approach this guard used to take). That
    risk is closed by requiring the two call sites to be the SAME source expression,
    not merely two expressions that might evaluate the same. Two identical expressions
    evaluated against the same `method` input cannot diverge -- there is no way for
    `ProcessEnvelope.versions(task.process, backend.versionTools)` to name a different
    tool list than `ProcessEnvelope.versionsStub(task.process, backend.versionTools)`
    for the same task, because they read the same variable. That is exactly Task 3's
    warp_seg_qc.nf shape: `backend` is resolved once per task from the `method` input,
    and both the script: and stub: blocks pass `backend.versionTools` verbatim.

    What is NOT allowed: a non-literal expression that appears in only one of the two
    blocks (an unpaired call), or two non-literal expressions that differ at all --
    including a ternary naming two *different* literal lists per branch, which is
    exactly the shape that would defeat a same-branch-only literal check.
    """
    unparseable = []
    for nf in MODULES:
        text = nf.read_text()
        calls = _extract_calls(text)
        script_args = [args for method, args in calls if method == "versions"]
        stub_args = [args for method, args in calls if method == "versionsStub"]

        non_literal_script = [a for a in script_args if a is None or not LITERAL_ARGS_RE.match(a)]
        non_literal_stub = [a for a in stub_args if a is None or not LITERAL_ARGS_RE.match(a)]
        if not non_literal_script and not non_literal_stub:
            continue

        # Symmetric-non-literal exception: exactly one non-literal versions() call and
        # exactly one non-literal versionsStub() call, sharing the module with no other
        # (literal or non-literal) calls, and their argument text matches exactly.
        symmetric = (
            len(script_args) == 1
            and len(stub_args) == 1
            and non_literal_script == script_args
            and non_literal_stub == stub_args
            and script_args[0] is not None
            and stub_args[0] is not None
            and _norm(script_args[0]) == _norm(stub_args[0])
        )
        if symmetric:
            continue

        for args in non_literal_script:
            unparseable.append((nf.name, "versions", args))
        for args in non_literal_stub:
            unparseable.append((nf.name, "versionsStub", args))

    assert not unparseable, (
        f"ProcessEnvelope call(s) that are neither a literal `task.process, [...]` tool "
        f"list NOR a non-literal expression textually IDENTICAL to its script:/stub: "
        f"counterpart, so test_script_and_stub_ask_for_the_same_tool_list cannot verify "
        f"them: {unparseable}. A ternary or a variable tool list must be the exact same "
        f"expression in both versions() and versionsStub(), with no other calls in the "
        f"module, or it needs its own explicit case in this guard rather than a silent "
        f"pass-through."
    )


def test_script_and_stub_ask_for_the_same_tool_list():
    """The failure this whole refactor exists to prevent: two lists, one invisible.

    Literal calls are compared by their parsed tool list. Non-literal calls are not
    re-parsed here -- test_versions_calls_are_literal_or_symmetric_between_script_and_stub
    already proved any non-literal pair present is textually identical, which is the
    strongest equality this guard can assert without evaluating Groovy -- so a module
    using only the symmetric non-literal shape has nothing left to compare and is
    skipped, exactly like a module with no ProcessEnvelope calls at all.
    """
    mismatches = []
    for nf in MODULES:
        text = nf.read_text()
        calls = _extract_calls(text)
        script_calls = [
            LITERAL_ARGS_RE.match(args).group(1) for method, args in calls if method == "versions" and args and LITERAL_ARGS_RE.match(args)
        ]
        stub_calls = [
            LITERAL_ARGS_RE.match(args).group(1) for method, args in calls if method == "versionsStub" and args and LITERAL_ARGS_RE.match(args)
        ]
        if not script_calls and not stub_calls:
            continue

        def norm(xs):
            return [re.sub(r"\s+", "", x) for x in xs]

        if norm(script_calls) != norm(stub_calls):
            mismatches.append((nf.name, script_calls, stub_calls))
    assert not mismatches, (
        f"script: and stub: pass different tool lists in {[m[0] for m in mismatches]}: {mismatches}"
    )
