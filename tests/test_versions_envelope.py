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


def test_versions_calls_pass_a_literal_tool_list():
    """Every ProcessEnvelope.versions()/versionsStub() call must be parseable into a
    literal `task.process, ['a', 'b']` tool list, or the guard below that compares
    script: against stub: cannot see it at all -- see _extract_calls' docstring.
    """
    unparseable = []
    for nf in MODULES:
        text = nf.read_text()
        for method, args in _extract_calls(text):
            if args is None or not LITERAL_ARGS_RE.match(args):
                unparseable.append((nf.name, method, args))
    assert not unparseable, (
        f"ProcessEnvelope call(s) that are not a literal `task.process, [...]` tool "
        f"list, so test_script_and_stub_ask_for_the_same_tool_list cannot compare "
        f"them: {unparseable}. A ternary or a variable tool list needs its own "
        f"explicit case in this guard, not a silent pass-through."
    )


def test_script_and_stub_ask_for_the_same_tool_list():
    """The failure this whole refactor exists to prevent: two lists, one invisible."""
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
