"""Every intermediate publishDir is gated on --cleanup_level; no final one is.

The gate is the inlined literal `params.cleanup_level == 'none'`, repeated at
each site. That repetition is NOT an oversight: conf/*.config cannot see
lib/*.groovy, and under Nextflow 26's strict parser there is no legal way to
share a helper between two `withName:` blocks -- a top-level `def` fails to
parse and a top-level closure assignment is rejected as a dynamic option. So
the seam CLAUDE.md permits is: inline it verbatim, and assert on the config
text through the shared source model. This file is that assertion, and
Layout.FINAL_KINDS -- read here rather than restated -- is what the copies are
held to.

Checked PER publishDir ENTRY, not per block. A `withName:` block's publishDir
is often a LIST of maps with different destinations, and REGISTER is exactly
that case: one map publishes the registered slides (an intermediate) and
another publishes registered/summary (small text provenance that survives every
level). A block-level `'params.cleanup_level' in block.body` check would call
that block gated on the strength of either one.
"""
import re

from tests.nfmodel import (
    REPO_ROOT,
    strip_comments,
    strip_comments_and_strings,
    with_name_blocks,
)

GATE = "params.cleanup_level == 'none'"

_PUBLISH_PATH = re.compile(r'path:\s*\{\s*"\$\{params\.outdir\}([^"]*)"')
_LAYOUT = strip_comments((REPO_ROOT / "lib" / "Layout.groovy").read_text())


def _groovy_list(name):
    m = re.search(rf"{name}\s*=\s*\[(.*?)\]", _LAYOUT, re.S)
    assert m, f"{name} missing from Layout"
    return set(re.findall(r"'([^']+)'", m.group(1)))


def _leaf(path_expr):
    """The per-patient publish leaf, or None for a run-level path."""
    if "${meta.patient_id}" not in path_expr:
        return None
    tail = path_expr.split("${meta.patient_id}", 1)[1].strip("/")
    return tail.split("/")[0] if tail else None


def _enclosing_map(brackets: str, text: str, pos: int) -> str:
    """The `[ ... ]` map literal containing offset `pos`.

    `brackets` is the strings-AND-comments-blanked view, used ONLY for
    balancing (a bracket inside a quoted pattern or a comment must not count);
    `text` is the strings-intact view the result is sliced from. tests/nfmodel
    preserves length in both, so one offset indexes either.
    """
    depth, i = 0, pos
    while i > 0:
        i -= 1
        if brackets[i] == "]":
            depth += 1
        elif brackets[i] == "[":
            if depth == 0:
                break
            depth -= 1
    start = i
    depth, j = 1, start + 1
    while j < len(brackets) and depth:
        depth += (brackets[j] == "[") - (brackets[j] == "]")
        j += 1
    return text[start:j]


def _publish_entries():
    """(selector, line, path_expr, leaf, entry_text) per publishDir destination."""
    for block in with_name_blocks():
        text = strip_comments(block.raw_body)          # strings intact
        brackets = strip_comments_and_strings(block.raw_body)  # safe to balance
        for m in _PUBLISH_PATH.finditer(text):
            yield (
                block.selector,
                block.start_line,
                m.group(1),
                _leaf(m.group(1)),
                _enclosing_map(brackets, text, m.start()),
            )


def test_the_scan_is_not_vacuous():
    entries = list(_publish_entries())
    assert len(entries) > 10, (
        f"only {len(entries)} publish entries found -- the extractor is stale. "
        "Note the path is inside a STRING, so the fully-stripped view finds none."
    )
    assert any(leaf for *_r, leaf, _t in entries), "no per-patient leaves found"
    # Every entry must have resolved to a real map, or the gate lookups below
    # are all searching the empty string and everything passes.
    for selector, line, path_expr, _leaf_, entry in entries:
        assert "path:" in entry, (
            f"conf/modules.config:{line} '{selector}': could not slice the "
            f"enclosing map for {path_expr}"
        )


def test_every_intermediate_publish_is_gated():
    final = _groovy_list("FINAL_KINDS")
    published = _groovy_list("PUBLISHED_KINDS")
    ungated = []
    for selector, line, path_expr, leaf, entry in _publish_entries():
        if leaf is None or leaf not in published or leaf in final:
            continue
        # registered/manifest and registered/summary are small text provenance
        # with their own entries; they survive every level by design, and the
        # QC report links to them.
        if path_expr.rstrip("/").endswith(("manifest", "summary")):
            continue
        if GATE not in entry:
            ungated.append(
                f"conf/modules.config:{line} withName: '{selector}' publishes "
                f"intermediate '{leaf}' ({path_expr}) with no cleanup gate"
            )
    assert not ungated, "\n".join(ungated) + (
        f"\n\nAdd `enabled: {{ {GATE} }}` to that publishDir entry. "
        "Layout.FINAL_KINDS decides which leaves are final."
    )


def test_no_final_publish_is_gated():
    """A gate on a final artifact would withhold the thing the run is FOR."""
    final = _groovy_list("FINAL_KINDS")
    wrong = [
        f"conf/modules.config:{line} withName: '{selector}' gates FINAL "
        f"artifact '{leaf}' ({path_expr})"
        for selector, line, path_expr, leaf, entry in _publish_entries()
        if leaf in final and GATE in entry
    ]
    assert not wrong, "\n".join(wrong)


def test_run_level_and_qc_paths_are_never_gated():
    """qc/, csv/ and size_logs/ are provenance and reporting, and the
    per-patient qc/ subtree is the report the run is judged by. None of them is
    image payload, and all of them survive every level."""
    wrong = []
    for selector, line, path_expr, leaf, entry in _publish_entries():
        segments = [s for s in path_expr.strip("/").split("/") if s]
        is_qc = "qc" in segments or "size_logs" in segments
        if (leaf is None or is_qc) and GATE in entry:
            wrong.append(
                f"conf/modules.config:{line} withName: '{selector}' gates "
                f"run-level or QC path {path_expr}"
            )
    assert not wrong, "\n".join(wrong)


def test_the_unrouted_fallthrough_is_never_gated():
    """_UNROUTED_PUBLISH exists to make an unrouted process VISIBLE. Hiding it
    at the default level would restore exactly the silent overwrite it was
    introduced to expose."""
    wrong = [
        f"conf/modules.config:{line} withName: '{selector}' gates the "
        f"_UNROUTED_PUBLISH fall-through"
        for selector, line, path_expr, _leaf_, entry in _publish_entries()
        if "_UNROUTED_PUBLISH" in path_expr and GATE in entry
    ]
    assert not wrong, "\n".join(wrong)


def test_the_gate_text_is_identical_everywhere():
    """Twelve inlined copies of one predicate. They cannot be shared, so the
    only thing standing between them and drift is that they are compared -- a
    site that read `!= 'final'` would behave identically today and differently
    the moment a third level is added."""
    # Only the CLEANUP gates. `publishDir = [ enabled: false ]` appears seven
    # times as a deliberate "this process publishes nothing" marker (SEG_QC's
    # per-slide masks, the solve steps' intermediates) and predates all of this;
    # folding those in would make this assertion about something else entirely.
    variants = {
        " ".join(v.split()).rstrip("]").strip()
        for v in re.findall(r"enabled:\s*([^,\n]+)", strip_comments(
            (REPO_ROOT / "conf" / "modules.config").read_text()
        ))
        if "cleanup_level" in v
    }
    assert variants, "no cleanup gate found in conf/modules.config at all"
    assert variants == {GATE}, (
        f"publishDir cleanup gates have drifted apart: {sorted(variants)}"
    )


def test_no_gate_is_written_as_a_closure():
    """`enabled: { ... }` is silently WRONG, and this is the only thing that
    would catch it.

    Nextflow does not resolve a closure in publishDir's `enabled:` option the
    way it resolves one in `path:` or `saveAs:`. The closure OBJECT is coerced
    instead, and it coerces to false -- so every gated publish is dropped at
    EVERY level, including --cleanup_level=none.

    Measured 2026-08-25 on NXF_VER=26.04.6, stub, one patient:

        enabled: { params.cleanup_level == 'none' }   ->  30 files at BOTH levels
        enabled: params.cleanup_level == 'none'       ->  30 at final, 62 at none

    62 is the pre-change baseline. The closure form silently made
    `--cleanup_level=none` a no-op, which would have broken every `--start
    <step>` and every add_cycle chain -- while all the static guards above, and
    a normal `-profile test` run, passed. It is the form the plan specified,
    which is why it is worth a test rather than a comment.

    **How to reproduce those two numbers, and why a naive attempt does not.**
    Re-measured 2026-09-01, same engine (NXF_VER=26.04.6), same stub, same one
    patient. `git archive`-ing the tree at 4a6134e reproduces 30 / 62 exactly.
    At HEAD the same two runs give **29 / 64**, and every file of that drift is
    accounted for: `+P001_registrar.pickle` (6823d4b) and
    `+qc/preflight_scale_report.json` (9d786bd) at both levels, and at `final`
    the four `csv/*.csv` checkpoint manifests were replaced by the single
    `csv/README.txt` once no manifest was written at a cleaning level
    (30 + 2 - 4 + 1 = 29; 62 + 2 = 64). The numbers above are a dated record of
    one commit, not a live assertion -- nothing here asserts a file count.

    The trap, measured the same day and the reason a re-measurement looks like
    a regression: **the level has to arrive on the command line or in a
    -params-file. A profile pin does not reach these gates.**

        -profile test                          (conf/test.config pins 'none') ->  32
        -profile test --cleanup_level none                                    ->  64
        -profile test -params-file {cleanup_level: none}                      ->  64
        -profile test --cleanup_level final                                   ->  29

    All four ran the same 35 tasks with the same effective `cleanup_level`.
    `enabled:` is a plain boolean, so it is evaluated EAGERLY when
    `conf/modules.config` is parsed -- and `nextflow.config` includes that file
    (line 532) BEFORE it declares `profiles {}` (line 581), so a profile's
    `params.cleanup_level` is not yet set and every gate freezes against the
    shipped `'final'`. That is the price of the plain-boolean form the test
    below enforces, and it is not a reason to go back: the closure form gates
    nothing at all, at any level, by any route.
    """
    offenders = [
        f"conf/modules.config: `enabled: {{{v.strip()}}}` is a closure and will "
        f"coerce to false at every cleanup level"
        for v in re.findall(r"enabled:\s*\{([^}]*)\}", strip_comments(
            (REPO_ROOT / "conf" / "modules.config").read_text()
        ))
    ]
    assert not offenders, "\n".join(offenders) + (
        "\n\nWrite it as a plain boolean expression: "
        f"`enabled: {GATE}`"
    )
