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
        text = strip_comments(block.raw_body)  # strings intact
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
        f"\n\nAdd `saveAs: {{ fn -> {GATE} ? fn : null }}` to that publishDir entry. "
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
    # Every occurrence of the predicate, wherever it sits. `publishDir =
    # [ enabled: false ]` appears as a deliberate "this process publishes
    # nothing" marker (SEG_QC's per-slide masks, the solve steps'
    # intermediates) and never names cleanup_level, so it is not matched.
    variants = {
        " ".join(v.split())
        for v in re.findall(
            r"params\.cleanup_level\s*[!=]=\s*'[^']*'",
            strip_comments((REPO_ROOT / "conf" / "modules.config").read_text()),
        )
    }
    assert variants, "no cleanup gate found in conf/modules.config at all"
    assert variants == {GATE}, (
        f"publishDir cleanup gates have drifted apart: {sorted(variants)}"
    )


def test_the_gate_is_lazy_never_an_eager_enabled_boolean():
    """The gate must be evaluated PER FILE, at publish time -- inside `saveAs:`
    -- never as an `enabled:` boolean computed when the config is parsed.

    `enabled:` cannot be right in either form:

      * `enabled: { ... }` (a closure) is not resolved the way `path:`/`saveAs:`
        closures are; the closure OBJECT is coerced and comes out false, so every
        gated publish is dropped at EVERY level. Measured 2026-08-25 on
        NXF_VER=26.04.6: 30 files at both levels.
      * `enabled: params.cleanup_level == 'none'` (a plain boolean) is evaluated
        EAGERLY at the line it appears on, and nextflow.config includes
        conf/modules.config BEFORE it declares `profiles {}` and before any `-c`
        file is merged. So the gate freezes against the SHIPPED 'final' and no
        profile or site-config pin ever reaches it -- while the resolved config
        prints `cleanup_level = 'none'` and every static guard passes. Measured
        2026-09-06 on main @ c201f443, stub, one patient:

            -profile test                    (conf/test.config pins 'none') -> 32
            -profile test -c site.config     (params { cleanup_level='none' }) -> 32
            -profile test --cleanup_level none                                -> 64

        The README routes every external operator through `-c site.config`, and
        'none' is what makes `--start` and add_cycle re-entry possible. That was
        the plain-boolean form's price, and it was too high.

    `saveAs:` is a closure Nextflow resolves per published file, in a context
    where `params` is the FINAL merged map. `{ fn -> <GATE> ? fn : null }` keeps
    the file at 'none' and skips it (null) at every other level, and it
    composes with the existing versions.yml filter in the same closure. The
    behavioural counterpart -- three stub runs through the profile, a `-c`
    pin and a `-c` override -- is tests/cleanup_level_pin.sh.
    """
    config = strip_comments((REPO_ROOT / "conf" / "modules.config").read_text())
    eager = [
        f"conf/modules.config: `enabled: {v.strip()}` evaluates when the config "
        "is parsed, before profiles and -c files merge -- the pin never reaches it"
        for v in re.findall(r"enabled:\s*([^,\n]+)", config)
        if "cleanup_level" in v
    ]
    assert not eager, "\n".join(eager) + (
        f"\n\nMove the predicate into saveAs: `saveAs: {{ fn -> {GATE} ? fn : null }}`"
    )
    not_lazy = []
    for selector, line, path_expr, _leaf_, entry in _publish_entries():
        if GATE not in entry:
            continue
        m = re.search(r"saveAs:\s*\{([^}]*)\}", entry)
        if not m or GATE not in m.group(1):
            not_lazy.append(
                f"conf/modules.config:{line} withName: '{selector}' gates "
                f"{path_expr} outside a saveAs closure"
            )
    assert not not_lazy, "\n".join(not_lazy)
