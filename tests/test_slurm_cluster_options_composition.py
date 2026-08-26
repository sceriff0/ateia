#!/usr/bin/env python3
"""Guard: a `withName:` block that sets `clusterOptions` must not drop --account/--qos.

`nextflow.config`'s `slurm` profile sets `process.clusterOptions` (around line 527) to a
closure composing `--account=${params.slurm_account}` and `--qos=${params.slurm_qos}` from
the two slurm params, using a null-vs-empty-string convention: each option is added only
when its param is truthy and non-empty, and the whole thing evaluates to `null` (not '')
when neither applies.

A Nextflow `withName:` field assignment REPLACES the process-scope value for that field --
it does not merge with it. So a `withName:` block that sets its OWN `clusterOptions` (e.g.
`SEGMENT`, to request a GPU via `--gres`) silently drops the slurm profile's --account/--qos
composition for that one process. On a cluster that requires an account or a QoS, that job is
rejected at submission or lands in the wrong accounting bucket -- and it does so silently:
there is no error, just a job that either bounces or gets billed to the wrong place.

This bit `SEGMENT` (and `SEG_QC_SEGMENT`, which matches the same `withName: 'SEGMENT'`
selector via Nextflow's alias-matching -- see the long comment in conf/modules.config just
above that block): its `clusterOptions` closure only ever emitted `--gres=gpu:...`.

The fix is to INLINE the account/qos composition into every `withName:` block that sets
`clusterOptions`, rather than sharing a helper -- conf/modules.config already documents at
length that a config file cannot declare a shared helper under Nextflow 26's strict parser
(a top-level `def` is `Unexpected input: '('`; a top-level closure binding is `Dynamic config
options only allowed in process scope`), and reading `task.clusterOptions` inside the closure
to append to it recurses (task.clusterOptions resolves through the very closure being
evaluated).

This guard asserts every `withName:` block that sets `clusterOptions` also references BOTH
`params.slurm_account` and `params.slurm_qos` in that block's raw text -- i.e. it composes the
account/QoS options rather than replacing them wholesale. It deliberately reads the RAW block
body, not a comment-and-string-blanked view (`tests/_code_view.py`'s `code_view()`): the thing
under assertion here is `params.slurm_account`/`params.slurm_qos` appearing inside a quoted
GString literal (`"--account=${params.slurm_account}"`), and blanking string CONTENT would
erase exactly that.

Shape follows `tests/test_resource_label_coverage.py`'s `find_withname_resource_fields()`:
same `WITH_NAME_OPEN_RE` selector match, same brace-matching walk to find the block body
(a regex cannot balance the nested `{ }` a `clusterOptions` closure or a `publishDir` list
commonly contains), and the same kind of matcher self-test
(`test_withname_brace_matcher_handles_nested_closures` there) reproduced here as
`test_brace_matcher_handles_nested_closures_around_clusteroptions`.
"""

from __future__ import annotations

import re
from pathlib import Path

from _code_view import code_view as _code_view

ROOT = Path(__file__).resolve().parent.parent
MODULES_CONFIG = ROOT / "conf" / "modules.config"
NEXTFLOW_CONFIG = ROOT / "nextflow.config"

# `withName: 'A|B|C' {` -- opens a withName block. The name group can hold a `|`-separated
# alternation naming several processes at once. The block BODY is found separately by
# brace-matching from this match's end, because the body can contain nested `{ }` (closures,
# lists) a regex cannot balance.
WITH_NAME_OPEN_RE = re.compile(r"withName:\s*(?:'([^']+)'|\"([^\"]+)\")\s*\{")

# A top-level `clusterOptions =` assignment line inside a withName block body. Anchored to
# line-start (after indentation) so it only matches a real assignment, not a comment
# mentioning the word or an unrelated identifier.
CLUSTER_OPTIONS_FIELD_RE = re.compile(r"^\s*clusterOptions\s*=", re.MULTILINE)


def _brace_match(text: str, open_brace_index: int) -> str:
    """Return the body between `text[open_brace_index] == '{'` and its balanced partner."""
    assert text[open_brace_index] == "{"
    depth = 1
    pos = open_brace_index + 1
    while depth > 0 and pos < len(text):
        if text[pos] == "{":
            depth += 1
        elif text[pos] == "}":
            depth -= 1
        pos += 1
    return text[open_brace_index + 1 : pos - 1]


def _brace_span(text: str, open_brace_index: int) -> tuple[int, int]:
    """Return the (start, end) body span for `text[open_brace_index] == '{'`, exclusive of
    the braces themselves -- same walk as `_brace_match`, but returning offsets rather than
    a slice, so the identical span can be sliced out of a SECOND text of the same length
    (namely `code_view()`'s output, which blanks in place and preserves offsets)."""
    assert text[open_brace_index] == "{"
    depth = 1
    pos = open_brace_index + 1
    while depth > 0 and pos < len(text):
        if text[pos] == "{":
            depth += 1
        elif text[pos] == "}":
            depth -= 1
        pos += 1
    return open_brace_index + 1, pos - 1


def find_withname_cluster_options_blocks() -> list[tuple[str, int, str, str]]:
    """Return (selector, line_no, raw_body, blanked_body) for every `withName:` block that
    sets `clusterOptions`.

    Two views of the SAME span, for two different questions:
      * `raw_body` is the file's own text, comments and strings intact -- use it whenever the
        thing under assertion lives inside a quoted literal, which is exactly where
        `"--account=${params.slurm_account}"` lives. `code_view()` would still show
        `params.slurm_account` here too (GString interpolation is deliberately NOT masked),
        but `raw_body` is the more direct match and keeps this guard readable against the
        block's real text.
      * `blanked_body` is `code_view()`'s comment-and-string-content-blanked view of the same
        span (same offsets, since `code_view()` blanks in place) -- use it for an IDENTIFIER
        scan, so a comment merely mentioning `task.clusterOptions` in prose does not read as
        a live code reference. This guard's own conf/modules.config edit demonstrates why:
        the explanatory comment above the fixed `clusterOptions` closure literally contains
        the string `task.clusterOptions`, which made a raw-text-only recursion scan fail on
        the FIXED config -- a false positive from exactly the trap `code_view()` exists to
        avoid.

    `line_no` is the 1-based line of the `withName:` opener, for readable failure messages.
    """
    raw = MODULES_CONFIG.read_text()
    blanked = _code_view(MODULES_CONFIG)
    assert len(raw) == len(blanked), "code_view() must preserve offsets 1:1 with the raw text"

    blocks: list[tuple[str, int, str, str]] = []
    for match in WITH_NAME_OPEN_RE.finditer(raw):
        selector = match.group(1) if match.group(1) is not None else match.group(2)
        start, end = _brace_span(raw, match.end() - 1)
        raw_body = raw[start:end]
        if CLUSTER_OPTIONS_FIELD_RE.search(raw_body):
            line_no = raw.count("\n", 0, match.start()) + 1
            blocks.append((selector, line_no, raw_body, blanked[start:end]))
    return blocks


def test_brace_matcher_handles_nested_closures_around_clusteroptions():
    """Regression guard on the brace-matcher itself.

    A `clusterOptions = { ... }` closure can sit among other fields that themselves nest
    `{ }` (a `memory` closure, a `publishDir` list) -- a naive "first `}` closes the block"
    scan would truncate the body early and miss the `clusterOptions` field entirely, or a
    "last `}` in the file" scan would swallow an unrelated later block. Mirrors
    `tests/test_resource_label_coverage.py::test_withname_brace_matcher_handles_nested_closures`.
    """
    text = (
        "withName: 'FOO' {\n"
        "    memory = { 8.GB * task.attempt }\n"
        "    clusterOptions = { params.seg_gpu ? \"--gres=gpu:${params.gpu_type}\" : '' }\n"
        "    publishDir = [ path: { \"x\" }, mode: 'copy' ]\n"
        "}\n"
        "withName: 'BAR' {\n"
        "    cpus = 1\n"
        "}\n"
    )
    match = WITH_NAME_OPEN_RE.search(text)
    body = _brace_match(text, match.end() - 1)
    assert "BAR" not in body
    assert CLUSTER_OPTIONS_FIELD_RE.search(body) is not None


def test_finder_extracts_the_raw_body_not_a_blanked_view():
    """Regression guard: the finder must read RAW text, so a quoted `params.slurm_account`
    read inside a GString literal is visible to the caller.

    Using `tests/_code_view.py`'s `code_view()` here would be wrong: it blanks string
    CONTENT, which is exactly where `"--account=${params.slurm_account}"` lives -- with that
    view, every clusterOptions closure would look identical (all strings emptied) and the
    guard below would compare nothing.
    """
    text = (
        "withName: 'FOO' {\n"
        "    clusterOptions = { \"--account=${params.slurm_account}\" }\n"
        "}\n"
    )
    match = WITH_NAME_OPEN_RE.search(text)
    body = _brace_match(text, match.end() - 1)
    assert "params.slurm_account" in body


def test_nextflow_config_slurm_profile_composes_account_and_qos():
    """Sanity-check the convention this guard enforces elsewhere really exists here.

    If this ever stops matching, the guard below is enforcing a convention that no longer
    exists anywhere in the repo -- so it should fail loudly rather than pass vacuously.
    """
    text = NEXTFLOW_CONFIG.read_text()
    assert "params.slurm_account" in text
    assert "params.slurm_qos" in text


def test_every_clusteroptions_override_composes_account_and_qos():
    """Every `withName:` block that sets `clusterOptions` must also emit --account/--qos.

    A `withName:` assignment REPLACES the process-scope `clusterOptions` set by the `slurm`
    profile (nextflow.config, ~line 527) rather than adding to it. So a block that sets its
    own `clusterOptions` without also composing `params.slurm_account` /
    `params.slurm_qos` silently drops the account/QoS submission arguments for that one
    process -- exactly what happened to `SEGMENT` (and, via Nextflow's alias-matching onto
    the same `withName: 'SEGMENT'` selector, `SEG_QC_SEGMENT`): its `clusterOptions` closure
    emitted `--gres=gpu:...` only.

    This does not evaluate the closures (that needs a live Nextflow run) -- it is a static
    check that the composition logic is PRESENT in the block's own text, inlined per the
    no-shared-helper constraint conf/modules.config documents.
    """
    blocks = find_withname_cluster_options_blocks()

    assert blocks, (
        "No `withName:` block sets `clusterOptions` in conf/modules.config -- scan pattern "
        "may be stale (SEGMENT's block used to set this)."
    )

    offending = []
    for selector, line_no, raw_body, _blanked_body in blocks:
        missing = [
            param
            for param in ("params.slurm_account", "params.slurm_qos")
            if param not in raw_body
        ]
        if missing:
            offending.append(
                f"conf/modules.config:{line_no}: withName: '{selector}' sets "
                f"`clusterOptions` but its body does not reference {missing} -- it "
                "REPLACES process.clusterOptions (nextflow.config's slurm profile), so "
                "the account/QoS submission arguments are silently dropped for this "
                "process."
            )

    assert not offending, (
        f"{len(offending)} `withName:` block(s) override `clusterOptions` without "
        "composing --account/--qos:\n" + "\n".join(offending)
    )


def test_clusteroptions_override_does_not_read_task_clusteroptions():
    """`task.clusterOptions` resolves through the very closure being evaluated -- reading it
    inside a `withName:` block's own `clusterOptions` closure to "append" to it recurses.

    This is the trap the fix must avoid: the composition has to be INLINED (matching
    nextflow.config's own account/qos logic verbatim at each override site), not built by
    reading back the value this same closure is in the middle of producing.

    Scanned on the BLANKED (comment-and-string-blanked) view deliberately, not the raw one:
    the fixed `SEGMENT` block's own explanatory comment names `task.clusterOptions` in prose
    (documenting exactly this trap), and a raw-text scan for the identifier would trip on its
    own documentation. `code_view()` blanks that comment out while leaving a genuine code
    reference intact, which is exactly the distinction this check needs.
    """
    blocks = find_withname_cluster_options_blocks()
    offending = [
        f"conf/modules.config:{line_no}: withName: '{selector}' reads `task.clusterOptions` "
        "inside its own clusterOptions closure -- this recurses."
        for selector, line_no, _raw_body, blanked_body in blocks
        if "task.clusterOptions" in blanked_body
    ]
    assert not offending, "\n".join(offending)


def test_recursion_scan_ignores_a_comment_but_catches_real_code():
    """Regression guard on the choice to scan `blanked_body`, not `raw_body`, for the
    recursion trap.

    Demonstrates both directions on a synthetic block: a comment merely naming
    `task.clusterOptions` must not trip the check, and an actual code reference must.
    """
    import tempfile

    from _code_view import code_view as code_view_fn

    commented = (
        "withName: 'FOO' {\n"
        "    // reading task.clusterOptions here would recurse\n"
        "    clusterOptions = { \"--account=${params.slurm_account}\" }\n"
        "}\n"
    )
    live = (
        "withName: 'FOO' {\n"
        "    clusterOptions = { task.clusterOptions + ' --extra' }\n"
        "}\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".config", delete=False) as fh:
        fh.write(commented)
        commented_path = Path(fh.name)
    with tempfile.NamedTemporaryFile("w", suffix=".config", delete=False) as fh:
        fh.write(live)
        live_path = Path(fh.name)
    try:
        assert "task.clusterOptions" in commented  # present in raw text...
        assert "task.clusterOptions" not in code_view_fn(commented_path)  # ...but not blanked
        assert "task.clusterOptions" in code_view_fn(live_path)  # real code survives blanking
    finally:
        commented_path.unlink()
        live_path.unlink()
