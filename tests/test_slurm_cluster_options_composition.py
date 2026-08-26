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
`params.slurm_account` and `params.slurm_qos` in that block's text -- i.e. it composes the
account/QoS options rather than replacing them wholesale.

Repointed at `tests.nfmodel` (2026-08-26) rather than carrying its own brace-matching parse
of `conf/modules.config` -- `tests/test_nfmodel.py::test_no_guard_parses_nextflow_source_privately`
is a discovery-based meta-test that forbids new private parses of Nextflow source, and its
`UNREPOINTED_NF_SOURCE_READERS` allowlist is shrink-only debt for *pre-existing* guards, not
somewhere a new file gets to join.

Deliberately reads TWO DIFFERENT views per assertion, and getting this backwards silently
makes the content check compare equal for every block:
  * The account/qos COMPOSITION check needs to see what is *inside* the closure's quoted
    literal (`"--account=${params.slurm_account}"`), so it scans `strip_comments(block.raw_body)`
    -- comments blanked, string contents (GString interpolation included) kept verbatim. Using
    `block.body` (comments AND strings blanked) here would blank away the very
    `params.slurm_account` text this check is looking for, inside every block, making the
    check vacuously pass or fail uniformly regardless of what any block actually composes.
  * The `task.clusterOptions` RECURSION check must not be fooled by a comment merely naming
    it -- conf/modules.config's own fixed `SEGMENT` block carries an explanatory comment that
    literally contains the string `task.clusterOptions`, documenting the trap this check
    exists to catch. So this check scans `block.body`, `with_name_blocks()`'s
    comment-and-string-blanked view, where that comment (and any string literal) is blanked to
    spaces but a real code reference survives.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.nfmodel import strip_comments as _strip_comments
from tests.nfmodel import strip_comments_and_strings as _strip_comments_and_strings
from tests.nfmodel import with_name_blocks as _with_name_blocks

ROOT = Path(__file__).resolve().parent.parent
NEXTFLOW_CONFIG = ROOT / "nextflow.config"

# A top-level `clusterOptions =` assignment line inside a withName block body. Anchored to
# line-start (after indentation) so it only matches a real assignment, not a comment
# mentioning the word or an unrelated identifier. Run against `block.body` (the
# comment-and-string-blanked view): a real assignment's identifier is unaffected by
# blanking, while a `// clusterOptions = ...` mention in a comment is blanked away and
# correctly does not match.
CLUSTER_OPTIONS_FIELD_RE = re.compile(r"^\s*clusterOptions\s*=", re.MULTILINE)


def find_withname_cluster_options_blocks():
    """Every `tests.nfmodel.WithNameBlock` from `conf/modules.config` whose body sets
    `clusterOptions`."""
    return [block for block in _with_name_blocks() if CLUSTER_OPTIONS_FIELD_RE.search(block.body)]


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

    Scans `strip_comments(block.raw_body)` -- comments blanked, string contents kept -- since
    the composed `params.slurm_account`/`params.slurm_qos` text this check looks for lives
    inside a quoted GString literal, which the fully-blanked `block.body` view would erase.

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
    for block in blocks:
        content = _strip_comments(block.raw_body)
        missing = [
            param
            for param in ("params.slurm_account", "params.slurm_qos")
            if param not in content
        ]
        if missing:
            offending.append(
                f"conf/modules.config:{block.start_line}: withName: '{block.selector}' sets "
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

    Scanned on `block.body`, `with_name_blocks()`'s comment-and-string-blanked view,
    deliberately -- not `strip_comments(block.raw_body)`: the fixed `SEGMENT` block's own
    explanatory comment names `task.clusterOptions` in prose (documenting exactly this trap),
    and a view that keeps comments intact would trip on its own documentation. The fully
    blanked view erases that comment while leaving a genuine code reference intact, which is
    exactly the distinction this check needs.
    """
    blocks = find_withname_cluster_options_blocks()
    offending = [
        f"conf/modules.config:{block.start_line}: withName: '{block.selector}' reads "
        "`task.clusterOptions` inside its own clusterOptions closure -- this recurses."
        for block in blocks
        if "task.clusterOptions" in block.body
    ]
    assert not offending, "\n".join(offending)


def test_recursion_scan_ignores_a_comment_but_catches_real_code():
    """Regression guard on the choice to scan the comment-and-string-blanked view, not the
    string-preserving one, for the recursion trap.

    Demonstrates both directions on synthetic text: a comment merely naming
    `task.clusterOptions` must not trip the check once blanked, and an actual code reference
    must survive blanking and still trip it.
    """
    commented = (
        "withName: 'FOO' {\n"
        "    // reading task.clusterOptions here would recurse\n"
        "    clusterOptions = { \"--account=${params.slurm_account}\" }\n"
        "}\n"
    )
    live = "withName: 'FOO' {\n    clusterOptions = { task.clusterOptions + ' --extra' }\n}\n"

    assert "task.clusterOptions" in commented  # present in raw text...
    assert "task.clusterOptions" not in _strip_comments_and_strings(commented)  # ...but blanked
    assert "task.clusterOptions" in _strip_comments_and_strings(live)  # real code survives
