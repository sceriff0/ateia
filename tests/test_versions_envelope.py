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

from tests.nfmodel import processes, strip_comments

ROOT = Path(__file__).resolve().parent.parent
MODULES = sorted((ROOT / "modules" / "local").glob("*.nf"))

# The processes this file governs: modules/local only.
#
# modules/nf-core/basicpy/main.nf is vendored and is deliberately out of scope. Read
# 2026-09-02: it has no versions.yml heredoc at all (it emits `topic: versions` value
# tuples, the nf-core convention) and no size log, so every rule below would be
# vacuous on it -- and holding vendored code to this repo's envelope is the same
# mistake ruff.toml's extend-exclude already refuses to make.
LOCAL_PROCESSES = [p for p in processes().values() if p.path.parent.name == "local"]

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
# There is no exception left. aggregate_size_logs.nf was the last one: it runs in
# `container 'ubuntu:22.04'` with no Python interpreter, and ProcessEnvelope always
# prepended a `python:` row, so routing it through versions() would have run
# `python --version 2>&1` and written the shell's own "command not found" message
# into a published report as a version number. ProcessEnvelope.versionsBash() renders
# a `bash:` row instead, so all 28 modules under modules/local/ now go through the
# class and this check covers every one of them.


def test_no_module_hand_writes_a_versions_heredoc():
    offenders = []
    for nf in MODULES:
        text = nf.read_text()
        if "END_VERSIONS" in text and "ProcessEnvelope." not in text:
            offenders.append(nf.name)
    assert not offenders, (
        f"{offenders} hand-write a versions.yml heredoc. Use "
        "ProcessEnvelope.versions()/versionsStub() so the script: and stub: copies "
        "cannot name different tools."
    )


CALL_START_RE = re.compile(r"ProcessEnvelope\.(versions|versionsStub)\(")

# A literal tools list, with the mandatory `task.container` third argument:
# `task.process, ['a', 'b'], task.container`.
#
# THE THIRD ARGUMENT HAD TO BE ADDED HERE, not left to the symmetric-non-literal
# escape hatch below. Without it every 3-arg call became "non-literal", every module
# took the symmetric exception, and test_script_and_stub_ask_for_the_same_tool_list
# -- the check this entire file exists for -- silently had nothing left to compare in
# ANY module. Watched: with the old regex and the migrated modules, that test
# collected zero literal calls and passed while blind.
LITERAL_ARGS_RE = re.compile(
    r"^\s*task\.process\s*,\s*(\[\s*(?:'[^']*'\s*(?:,\s*'[^']*'\s*)*)?\])"
    r"\s*(?:,\s*task\.container\s*)?$"
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

        non_literal_script = [
            a for a in script_args if a is None or not LITERAL_ARGS_RE.match(a)
        ]
        non_literal_stub = [
            a for a in stub_args if a is None or not LITERAL_ARGS_RE.match(a)
        ]
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
            LITERAL_ARGS_RE.match(args).group(1)
            for method, args in calls
            if method == "versions" and args and LITERAL_ARGS_RE.match(args)
        ]
        stub_calls = [
            LITERAL_ARGS_RE.match(args).group(1)
            for method, args in calls
            if method == "versionsStub" and args and LITERAL_ARGS_RE.match(args)
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


# Every ProcessEnvelope entry point that renders a versions.yml heredoc. The bash
# pair takes no tools list, so it is matched here and nowhere else.
_VERSIONS_CALL_RE = re.compile(
    r"ProcessEnvelope\.(versions|versionsStub|versionsBash|versionsBashStub)\s*\("
)


def _split_top_level_commas(args):
    """Split a call's argument text on its TOP-LEVEL commas only.

    `task.process, ['tifffile', 'bioio'], task.container` is three arguments, not
    four: the comma inside the list literal is nested. A naive `args.split(",")`
    reports the last argument as `'bioio']` and this guard would fail every module
    that names two tools.
    """
    depth, out, cur = 0, [], ""
    for ch in args:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return [a.strip() for a in out]


def test_every_module_records_its_container():
    """Every versions.yml must name the image that produced it.

    Ruling R6 makes the container part of 1.0.0's reproducibility claim, and this
    repo's tags are content-descriptive (`:preprocess`, `:tiled`) rather than
    immutable -- so `container:` in versions.yml is the only record of WHICH image
    a given result came out of. `task.container` is non-null even with no container
    engine enabled (measured 2026-09-02 on 25.04.7), so there is no environment in
    which passing it is a no-op.

    Read off the COMMENT-STRIPPED view: this asks "does this call actually run?",
    and a comment quoting a 3-arg call would otherwise satisfy it while the real
    call ships two arguments. That mistake was made six times in one day on this
    repo (2026-09-01) and is recorded in CLAUDE.md.

    A module with ZERO ProcessEnvelope.versions*() calls in a half is not scanned by
    the loop below at all -- `_VERSIONS_CALL_RE.finditer` simply finds nothing, and an
    empty result reads exactly like "every call it found was fine". That is a real
    blind spot, not a hypothetical one: aggregate_size_logs.nf hand-wrote its own
    versions.yml heredoc (no ProcessEnvelope call, in either half) for as long as this
    test existed, and it was never once flagged as an offender by the loop below. The
    coverage check first closes that: every process must have at least one
    ProcessEnvelope.versions*() call in EACH half, or it is reported here explicitly
    rather than silently skipped.
    """
    offenders = []
    for proc in LOCAL_PROCESSES:
        for half in ("script_body", "stub_body"):
            text = strip_comments(getattr(proc, half))
            matches = list(_VERSIONS_CALL_RE.finditer(text))
            if not matches:
                offenders.append(
                    f"{proc.path.name}:{half}: no ProcessEnvelope.versions*() call at "
                    "all, so the container check below never sees this module"
                )
                continue
            for m in matches:
                args = _find_balanced_call_args(text, m.end() - 1)
                if args is None:
                    offenders.append(
                        f"{proc.path.name}:{half}: unbalanced {m.group(1)} call"
                    )
                    continue
                parts = _split_top_level_commas(args)
                if not parts or parts[-1] != "task.container":
                    offenders.append(
                        f"{proc.path.name}:{half}: {m.group(1)}({args.strip()}) does not "
                        "pass task.container as its last argument"
                    )
    assert not offenders, (
        "versions.yml must record the container image that produced it "
        "(00-master.md ruling R6):\n  " + "\n  ".join(offenders)
    )


def _blank_envelope_size_log_calls(text):
    """`text` with every ProcessEnvelope.sizeLog/sizeLogStub call's arguments blanked.

    The outFile argument of a legitimate call legitimately CONTAINS `size.csv`
    (`"${meta.id}.QUANTIFY.size.csv"`), so the "no hand-written row" rule below
    cannot be a plain substring check -- it would fail on the very calls it is
    asking for. Blanking the sanctioned calls first leaves exactly the
    unsanctioned mentions behind.
    """
    out = text
    while True:
        m = re.search(r"ProcessEnvelope\.(sizeLog|sizeLogStub)\s*\(", out)
        if not m:
            return out
        args = _find_balanced_call_args(out, m.end() - 1)
        if args is None:
            return out
        end = m.end() + len(args)
        out = out[: m.start()] + " " * (end - m.start()) + out[end:]


def test_every_size_log_module_uses_the_envelope():
    """A module that emits `size_log` renders BOTH its rows through ProcessEnvelope.

    The failure this closes is the size-log twin of the one the rest of this file
    closes for versions.yml, and it was worse: all 21 stub blocks wrote the literal
    `STUB` as the process name, so a stub CSV and a real CSV could not be compared
    at all, and bin/generate_resource_report.py carried a special case to drop those
    rows. `-stub` never evaluates `script:`, so nothing in the blocking gate could
    see the two halves disagree -- and they did: quantify.nf's script row keyed on
    meta.patient_id while its stub row keyed on meta.id.

    Discovery is by `emit: size_log`, not by a hard-coded list of 21 filenames: a
    new module that emits a size log is covered the day it is written.
    """
    offenders = []
    for proc in LOCAL_PROCESSES:
        if "emit: size_log" not in strip_comments(proc.outputs):
            continue
        for half, wanted in (
            ("script_body", "sizeLog("),
            ("stub_body", "sizeLogStub("),
        ):
            text = strip_comments(getattr(proc, half))
            if f"ProcessEnvelope.{wanted}" not in text:
                offenders.append(
                    f"{proc.path.name}:{half}: emits size_log but never calls "
                    f"ProcessEnvelope.{wanted}"
                )
            leftover = _blank_envelope_size_log_calls(text)
            if "size.csv" in leftover:
                offenders.append(
                    f"{proc.path.name}:{half}: writes a *.size.csv row outside "
                    "ProcessEnvelope.sizeLog/sizeLogStub"
                )
    assert not offenders, (
        "size-log rows must be rendered by lib/ProcessEnvelope.groovy so the "
        "script: and stub: copies cannot disagree:\n  " + "\n  ".join(offenders)
    )


SIZE_LOG_CALL_RE = re.compile(r"ProcessEnvelope\.(sizeLog|sizeLogStub)\s*\(")


def _extract_size_log_calls(text):
    """Every `ProcessEnvelope.sizeLog(...)` / `sizeLogStub(...)` call site in `text`,
    as (method, args_string) pairs -- the sizeLog/sizeLogStub twin of `_extract_calls`
    above, built on the same `_find_balanced_call_args` so a nested list argument
    (`sizeLog(task.process, meta.patient_id, ["${a}", "${b}"], outFile)`) is not
    truncated by a naive comma split.
    """
    calls = []
    for m in SIZE_LOG_CALL_RE.finditer(text):
        method = m.group(1)
        args = _find_balanced_call_args(text, m.end() - 1)
        calls.append((method, args))
    return calls


def test_size_log_and_stub_write_the_same_outfile():
    """sizeLog(...)'s and sizeLogStub(...)'s outFile -- each call's LAST positional
    argument -- must agree, call for call, between a module's script: and stub:
    halves.

    Nothing else enforces this: test_every_size_log_module_uses_the_envelope above
    only checks that both halves call *some* ProcessEnvelope size-log method, and
    test_script_and_stub_ask_for_the_same_tool_list is about the tools list argument
    to versions()/versionsStub(), a completely different pair of methods. A module
    whose script: writes `"${prefix}.FOO.size.csv"` and whose stub: writes
    `"${prefix}.BAR.size.csv"` would pass every existing guard in this file while a
    real run's size log and its stub-mode counterpart land at different filenames --
    invisible to `-stub`, which never evaluates script: at all (see this file's
    module docstring).

    Read off the comment-stripped view (`strip_comments`), not the raw script:/stub:
    text `Process.script_body`/`stub_body` carry -- a comment quoting a call with a
    different outFile would otherwise satisfy this the same way CLAUDE.md's
    "Verification reality" #7 describes for six other guards on 2026-09-01.

    Non-vacuity: at least 20 processes are expected to have a sizeLog call (measured
    21 on 2026-09-02) -- a collapse to a much smaller count would mean discovery
    itself broke, not that most modules stopped emitting size logs.
    """
    offenders = []
    compared = 0
    for proc in LOCAL_PROCESSES:
        script_calls = [
            args
            for method, args in _extract_size_log_calls(strip_comments(proc.script_body))
            if method == "sizeLog"
        ]
        stub_calls = [
            args
            for method, args in _extract_size_log_calls(strip_comments(proc.stub_body))
            if method == "sizeLogStub"
        ]
        if not script_calls and not stub_calls:
            continue
        compared += 1

        if len(script_calls) != len(stub_calls):
            offenders.append(
                f"{proc.path.name}: {len(script_calls)} sizeLog() call(s) in script: "
                f"vs {len(stub_calls)} sizeLogStub() call(s) in stub:"
            )
            continue

        for i, (s_args, st_args) in enumerate(zip(script_calls, stub_calls), start=1):
            if s_args is None or st_args is None:
                offenders.append(
                    f"{proc.path.name}: call #{i} has an unbalanced argument list"
                )
                continue
            s_out = _split_top_level_commas(s_args)[-1]
            st_out = _split_top_level_commas(st_args)[-1]
            if _norm(s_out) != _norm(st_out):
                offenders.append(
                    f"{proc.path.name}: call #{i} outFile differs -- "
                    f"sizeLog(..., {s_out}) vs sizeLogStub(..., {st_out})"
                )

    assert compared >= 20, (
        f"only {compared} process(es) had a sizeLog/sizeLogStub call to compare -- "
        "expected at least 20; discovery is likely broken"
    )
    assert not offenders, (
        "sizeLog() and sizeLogStub() must write to the SAME outFile so a stub run's "
        "size.csv can be compared against a real run's:\n  " + "\n  ".join(offenders)
    )
