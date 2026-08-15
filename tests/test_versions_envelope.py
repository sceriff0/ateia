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

# Documented exception -- not an oversight. There is exactly ONE now:
#
# segment.nf used to be the second. It hand-wrote both heredocs and they named DISJOINT
# key sets -- script: reported `python` plus the chosen backend's rows (`deepcell`/
# `tensorflow` for StarDist), stub: reported `python` plus a bare `seg_method: <name>`,
# which is not a tool at all. lib/SegBackends.groovy now stores each backend's tools as
# BARE MODULE NAMES (`versionTools`, same key as lib/WarpBackends.groovy) instead of
# pre-rendered YAML lines, so both blocks render through ProcessEnvelope from that one
# list and the three checks below cover it like any other module. Recorded here rather
# than deleted because "SegBackends stores rendered lines" was the stated reason this
# file allowed the exception, and that reason is now false.
#
# aggregate_size_logs.nf runs in `container 'ubuntu:22.04'` and reports a `bash:` version
# row, not a `python:` one -- it has no Python interpreter at all. ProcessEnvelope always
# prepends a `python:` row (23 of the 24 modules in modules/local/ report one; this is
# the genuine anomaly -- at the time ProcessEnvelope was introduced it was 27 of 28, and
# the four modules removed since -- compile_panel.nf, phenotype.nf, tiled_register.nf,
# warp_seg_qc_tiled.nf -- all reported `python:`), so routing this module through it
# would add a fabricated/`unknown` python entry to a published report that has never
# carried one.
# write_checkpoint_fragment.nf is the second, for the identical reason: it runs in
# `container 'ubuntu:22.04'` and its whole task is `cat > <patient>.csv`. There is no
# Python interpreter in that image, and ProcessEnvelope always prepends a `python:` row,
# so routing it through would put a fabricated (or `unknown`) python version into a
# published report for a process that never touched Python. It reports `bash:`, like
# aggregate_size_logs.nf.
#
# The script:/stub: divergence this guard exists to prevent is closed for that module by
# a different mechanism, not left open: BOTH blocks render their payload from one
# Checkpoint.fragment() call, which is the same "one list, two blocks" shape
# ProcessEnvelope applies to versions.yml. Only the two-line version heredoc is
# hand-written, and it names one tool.
# publish_passthrough.nf is the third and last, for that same reason: `ubuntu:22.04`,
# and its whole task is one `ln -s`. Its script:/stub: blocks differ in exactly the line
# this guard is about -- the `bash:` version row -- and in nothing else, deliberately,
# because there is nothing to stand in for: the file has to appear under
# registered_slides/ in a stub run too, since
# csv/registered.csv is written from the resulting path and tests/checkpoint_manifest.nf.test
# opens every path it names in exactly that mode.
ALLOWED_HANDWRITTEN = {
    "aggregate_size_logs.nf",
    "write_checkpoint_fragment.nf",
    "publish_passthrough.nf",
}


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


# Backend-resolution call sites: `def backend = WarpBackends.of(method)`,
# `def backend = SegBackends.of(params.seg_method)`, etc. Generalised past
# `WarpBackends` because the hole this closes is generic: any module that resolves a
# per-method backend object once per block and then forwards one of its fields
# (`backend.versionTools`) into ProcessEnvelope.
BACKEND_BINDING_RE = re.compile(r"(\w+)\s*=\s*(\w*Backends\.of\([^)\n]*\))")

# Splits a module's text at its `stub:` block boundary. One process per file (see
# CLAUDE.md's module conventions), so a single split is sufficient.
STUB_MARKER_RE = re.compile(r"\n(\s*)stub:\s*\n")


def _split_script_stub(text):
    """Return (script_half, stub_half): the module text before and from the `stub:`
    marker. If there's no stub: block, the whole text is the "script half" and the
    stub half is empty -- callers treat that as "no bindings in stub" honestly rather
    than accidentally searching the script half twice."""
    m = STUB_MARKER_RE.search(text)
    if not m:
        return text, ""
    return text[: m.start()], text[m.start() :]


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
    not merely two expressions that might evaluate the same. That is exactly Task 3's
    warp_seg_qc.nf shape: `backend` is resolved once per block from the `method` input,
    and both the script: and stub: blocks pass `backend.versionTools` verbatim.

    IDENTICAL CALL TEXT IS NOT ENOUGH ON ITS OWN, THOUGH. `backend.versionTools` and
    `backend.versionTools` are the same NAME in both blocks, not proof they are bound to
    the same VALUE: `backend` is a separate local assigned independently in script: and
    in stub:, and this function never looks at the assignment. `def backend =
    WarpBackends.of(method)` in script: paired with `def backend =
    WarpBackends.of('valis')` in stub: passes this check -- the two ProcessEnvelope call
    sites are byte-identical -- while silently stubbing the wrong backend's tool list on
    a tiled run. test_backend_binding_is_identical_between_script_and_stub below closes
    that residual hole by additionally comparing the `*Backends.of(...)` binding sites
    themselves, not just where their result is read.

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


def test_backend_binding_is_identical_between_script_and_stub():
    """Closes the hole the symmetric-non-literal exception above leaves open.

    A module that resolves a per-method backend once per block (`def backend =
    WarpBackends.of(method)`) and forwards one of its fields
    (`backend.versionTools`) into ProcessEnvelope passes the check above as soon as
    the two ProcessEnvelope call sites are textually identical -- but that only
    proves the two calls read the same NAME, not that `backend` was bound to the
    same backend in both blocks. `def backend = WarpBackends.of(method)` in
    script: paired with `def backend = WarpBackends.of('valis')` in stub: leaves
    `ProcessEnvelope.versionsStub(task.process, backend.versionTools)` byte-identical
    to its script: counterpart while silently reporting VALIS's tool list
    (`valis`/`skimage`/`scipy`) for a stub run of the tiled backend
    (`skimage`/`scipy`), and the check above cannot see it because it never looks
    past the call-site text to the binding.

    This test compares the `*Backends.of(...)` binding call sites themselves --
    split at the module's `stub:` marker so a script: binding is never compared
    against itself -- and fails if a module has bindings in both halves that are
    not textually identical (modulo whitespace).

    `modules/local/segment.nf` is the second module in scope: it binds
    `backend = SegBackends.of(params.seg_method)` in BOTH halves and forwards
    `backend.versionTools` to ProcessEnvelope from both. It used to bind only in script:
    and hand-write its stub heredoc, which is why this docstring previously named it as
    the reason for scoping to ProcessEnvelope users; that scoping is kept anyway, because
    a module that binds a backend for some other purpose entirely and never renders
    versions from it has nothing this test can meaningfully say about.
    """
    mismatches = []
    for nf in MODULES:
        text = nf.read_text()
        if "ProcessEnvelope." not in text:
            continue
        script_half, stub_half = _split_script_stub(text)
        script_bindings = BACKEND_BINDING_RE.findall(script_half)
        stub_bindings = BACKEND_BINDING_RE.findall(stub_half)
        if not script_bindings and not stub_bindings:
            continue
        if _norm(str(script_bindings)) != _norm(str(stub_bindings)):
            mismatches.append((nf.name, script_bindings, stub_bindings))
    assert not mismatches, (
        f"backend-binding call site(s) (e.g. `WarpBackends.of(...)`) differ between "
        f"script: and stub: in {[m[0] for m in mismatches]}: {mismatches}. A backend "
        f"must be resolved from the SAME expression (the same `method`/params argument) "
        f"in both blocks, or versionsStub() can report a different backend's tool list "
        f"than a real run of that task would -- the exact failure mode this whole file "
        f"exists to catch, one level removed from the ProcessEnvelope call site."
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


def test_a_comment_mentioning_process_envelope_does_not_satisfy_the_guard():
    """The guard must look for a CALL, not for the string `ProcessEnvelope.`.

    `test_no_module_hand_writes_a_versions_heredoc` used to ask
    `"ProcessEnvelope." not in text`. Any occurrence anywhere in the file satisfied
    that -- including a header comment saying "rendered through ProcessEnvelope."
    and including a commented-OUT call. A module could therefore hand-write both
    heredocs, describe itself as going through ProcessEnvelope, and pass. That
    happened: a module passed this guard silently until its header was reworded,
    at which point the guard suddenly failed and revealed it had never been
    checking anything about that file.

    The three plants below are the three ways the substring check was satisfiable
    without a single call being rendered. The fourth is the real thing, and must
    still be recognised -- a detector that says "no" to everything is not a fix.
    """
    heredoc = "cat <<-END_VERSIONS > versions.yml\nEND_VERSIONS\n"
    assert not _renders_versions_via_envelope(
        "// rendered through ProcessEnvelope.versions()\n" + heredoc
    ), "a line comment naming ProcessEnvelope must not count as rendering one"
    assert not _renders_versions_via_envelope(
        "/*\n * ProcessEnvelope.versions() renders both blocks.\n */\n" + heredoc
    ), "a block comment naming ProcessEnvelope must not count as rendering one"
    assert not _renders_versions_via_envelope(
        "    // ${ProcessEnvelope.versions(task.process, ['python'])}\n" + heredoc
    ), "a commented-OUT call must not count as rendering one"
    assert _renders_versions_via_envelope(
        "    ${ProcessEnvelope.versions(task.process, ['python'])}\n"
    ), "a real call must still be recognised"


def test_every_non_allowlisted_module_actually_calls_process_envelope():
    """The forward direction of the same fact, on the real tree.

    The guard above proves the detector rejects comments; this proves the detector
    does not reject everything. Every module outside ALLOWED_HANDWRITTEN must be
    seen as a genuine ProcessEnvelope renderer -- if the detector were broken shut,
    this fails for all of them at once rather than passing silently.
    """
    missing = [
        nf.name
        for nf in MODULES
        if nf.name not in ALLOWED_HANDWRITTEN
        and not _renders_versions_via_envelope(nf.read_text())
    ]
    assert not missing, (
        f"{missing} do not render versions.yml through ProcessEnvelope, and are not "
        "in ALLOWED_HANDWRITTEN."
    )
