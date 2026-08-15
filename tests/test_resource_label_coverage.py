#!/usr/bin/env python3
"""Guard against process labels that resolve to no `withLabel` selector.

A Nextflow `label` that matches no `withLabel:` selector is not an error --
the process silently falls through to `conf/base.config`'s defaults. That
makes an unmatched label invisible at runtime: it never crashes, it just
quietly under- (or over-) provisions the process.

This bit `modules/local/export_spatialdata.nf`, whose `label` line is a
ternary choosing between `'process_high_memory'` and `'process_medium'`.
`process_high_memory` was never defined in `conf/modules.config` -- it only
ever existed in CLAUDE.md's prose -- so the branch meant to request *more*
memory silently fell back to base.config's 6 GB default, 1/33rd of the
cheaper branch's 200 GB.

This test asserts every resource label referenced in `modules/local/*.nf`
resolves to a `withLabel` selector defined in `conf/modules.config`. Because
a `label` line can be an expression (a ternary, as above) rather than a bare
literal, extraction pulls *every* quoted string off a `label` line, not just
a single literal -- otherwise the guard would miss exactly the shape that
caused this bug.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULES_DIR = ROOT / "modules" / "local"
MODULES_CONFIG = ROOT / "conf" / "modules.config"

# A `label ...` statement, however its right-hand side is built (bare
# literal, ternary, etc.) -- anchored to line start so we don't match
# `label` appearing elsewhere (e.g. inside a string).
LABEL_LINE_RE = re.compile(r"^\s*label\s+.+$")

# Every single- or double-quoted string literal on a line.
QUOTED_STRING_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"")

# A trailing `// ...` line comment, or an inline `/* ... */` block comment.
# Stripped before label extraction: extraction deliberately takes *every*
# quoted string off a `label` line (to catch the ternary shape), so a quoted
# word inside a comment would otherwise be read as a live label. Measured:
# `label 'process_high'  // was 'process_low'` made the doc-table cross-check
# below pass while the process asked for 4x the cpus and ~10x the memory the
# doc promised -- a tier bump plus the comment explaining it silently
# disarmed the guard.
COMMENT_RE = re.compile(r"//.*$|/\*.*?\*/")

# `withLabel: 'name' { ... }` selectors in conf/modules.config.
WITH_LABEL_RE = re.compile(r"withLabel:\s*(?:'([^']+)'|\"([^\"]+)\")")

# `process NAME {` -- the process name declared by a modules/local/*.nf file.
PROCESS_NAME_RE = re.compile(r"^process\s+(\w+)\s*\{", re.MULTILINE)

# `withName: 'A|B|C' {` -- opens a withName block. The name group can hold a
# `|`-separated alternation naming several processes at once (see
# conf/modules.config:140 and :270); the block body is found separately by
# brace-matching from this match's end, because the body can itself contain
# nested `{ }` (closures, lists) that a regex cannot balance.
WITH_NAME_OPEN_RE = re.compile(r"withName:\s*(?:'([^']+)'|\"([^\"]+)\")\s*\{")

# A top-level `cpus =`, `memory =`, or `time =` assignment line inside a
# withName block body. Anchored to line-start (after indentation) so it
# only matches real assignments, not comments or unrelated identifiers.
RESOURCE_FIELD_RE = re.compile(r"^\s*(cpus|memory|time)\s*=", re.MULTILINE)

RESOURCE_FIELDS = frozenset({"cpus", "memory", "time"})


def extract_labels_from_line(line: str) -> list[str]:
    """Return every quoted string literal on a `label ...` line.

    A bare `label 'process_medium'` yields one label. A ternary like
    `label cond ? 'process_high_memory' : 'process_medium'` yields both
    branches -- this is the shape that produced the original bug, so any
    extraction that only handles a single literal would miss it.

    Comments are stripped first, so an annotation such as
    `label 'process_high'  // was 'process_low'` yields only the live label.
    Taking every quoted string is what makes the ternary work and what makes
    a commented-out label dangerous; the strip is the price of the former.
    """
    return [
        m.group(1) if m.group(1) is not None else m.group(2)
        for m in QUOTED_STRING_RE.finditer(COMMENT_RE.sub("", line))
    ]


def find_module_label_sites() -> list[tuple[Path, int, str, list[str]]]:
    """Return (file, line_no, line_text, labels) for every `label` line in modules/local/*.nf."""
    sites: list[tuple[Path, int, str, list[str]]] = []
    for path in sorted(MODULES_DIR.glob("*.nf")):
        for line_no, line in enumerate(path.read_text().splitlines(), start=1):
            if LABEL_LINE_RE.match(line):
                sites.append((path, line_no, line.strip(), extract_labels_from_line(line)))
    return sites


def find_defined_labels() -> set[str]:
    """Return the set of labels with a `withLabel:` selector in conf/modules.config."""
    text = MODULES_CONFIG.read_text()
    return {m.group(1) if m.group(1) is not None else m.group(2) for m in WITH_LABEL_RE.finditer(text)}


def test_extract_labels_handles_ternary_expression():
    """Extraction must find both branches of a ternary `label` line, not just one literal.

    This is a regression guard on the extractor itself: a naive
    `label\\s+'([^']+)'`-style pattern (single literal only) would silently
    return just one match -- or none -- for the exact line shape that
    caused the export_spatialdata.nf defect, defeating the guard below.
    """
    ternary_line = "    label params.spatialdata_include_image ? 'process_high_memory' : 'process_medium'"
    assert extract_labels_from_line(ternary_line) == ["process_high_memory", "process_medium"]

    literal_line = "    label 'process_high'"
    assert extract_labels_from_line(literal_line) == ["process_high"]


def test_extract_labels_ignores_commented_out_labels():
    """A quoted label inside a comment must not count as a live label.

    Measured on `modules/local/merge_quant_csvs.nf`: bumping
    `label 'process_low'` to `'process_high'` is caught by the doc-table
    cross-check below, but the *same* bump annotated
    `// was 'process_low'` was not -- the stale label came back out of the
    comment, matched the unchanged `docs/resources.md` row, and the guard
    went green while the process asked for 8 cpu / 300 GB against a doc
    promising 2 cpu / 32 GB.
    """
    assert extract_labels_from_line("    label 'process_high'  // was 'process_low'") == ["process_high"]
    assert extract_labels_from_line("    label /* 'process_low' */ 'process_high'") == ["process_high"]
    assert extract_labels_from_line("    label 'process_medium' // bumped, see #123") == ["process_medium"]


def test_every_module_label_resolves_to_a_conf_modules_config_selector():
    """Every label referenced in modules/local/*.nf must resolve in conf/modules.config.

    An unmatched label is not a Nextflow error -- the process silently
    inherits conf/base.config's defaults instead of the resources its label
    name implies. This is exactly how export_spatialdata.nf's
    'process_high_memory' branch OOM-prone-ly ran on a 6 GB default instead
    of a high-memory tier.
    """
    defined = find_defined_labels()
    sites = find_module_label_sites()

    assert sites, "No `label` lines found under modules/local/*.nf -- scan pattern may be stale."
    assert defined, "No `withLabel:` selectors found in conf/modules.config -- scan pattern may be stale."

    offending = []
    for path, line_no, line_text, labels in sites:
        for label in labels:
            if label not in defined:
                offending.append(
                    f"{path.relative_to(ROOT)}:{line_no}: label '{label}' has no "
                    f"`withLabel: '{label}'` selector in conf/modules.config "
                    f"(line: {line_text})"
                )

    assert not offending, (
        f"{len(offending)} label reference(s) resolve to no `withLabel` selector "
        "in conf/modules.config, so the process silently falls back to "
        "conf/base.config's defaults instead of the resources the label name "
        "implies. Define the selector or re-point the module at an existing "
        "label:\n" + "\n".join(offending)
    )


# ---------------------------------------------------------------------------
# Exactly-one-owner guard.
#
# A `withName:` block in conf/modules.config wins over a `label`'s
# `withLabel:` tier whenever both set the same field -- that is Nextflow's
# config-merge precedence, not a bug. So a module can end up with:
#   - a `label`  that is completely redundant (every one of cpus/memory/time
#     is also set by a matching `withName:` block) -- the label is dead
#     prose, and a maintainer editing it to "fix" the process's resources
#     would see no effect.
#   - NEITHER a `label` NOR full `withName:` coverage of cpus/memory/time --
#     whichever field nothing sets falls through to conf/base.config's small
#     defaults (1 cpu, 6 GB, 4 h -- see conf/base.config), silently
#     under-provisioning the task.
#
# Both are bugs; exactly one of {label, full withName coverage} must be true
# per process. A *partial* withName override (some but not all of
# cpus/memory/time -- e.g. TILED_SOLVE's withName block sets only
# `memory`, leaving `cpus`/`time` to the label's tier) is not a violation:
# the label is still the sole owner of the field(s) withName leaves alone,
# so removing it would silently change that field's value -- exactly the
# regression this guard exists to prevent, not permit.
# ---------------------------------------------------------------------------


def find_module_processes() -> dict[str, Path]:
    """Return {PROCESS_NAME: module_file_path} for every modules/local/*.nf file."""
    processes: dict[str, Path] = {}
    for path in sorted(MODULES_DIR.glob("*.nf")):
        match = PROCESS_NAME_RE.search(path.read_text())
        if match:
            processes[match.group(1)] = path
    return processes


def find_labelled_processes() -> set[str]:
    """Return the set of process names whose module file has a `label` line.

    LABEL_LINE_RE is anchored with bare `^`/`$` and deliberately compiled
    without `re.MULTILINE` (find_module_label_sites, above, matches it
    per-line via .match() on each splitlines() entry) -- so it must be
    applied the same way here, per line, not via .search() on the whole
    file text, or `^`/`$` would only ever anchor to the start/end of the
    entire string and silently never match.
    """
    labelled = set()
    for name, path in find_module_processes().items():
        if any(LABEL_LINE_RE.match(line) for line in path.read_text().splitlines()):
            labelled.add(name)
    return labelled


def find_withname_resource_fields() -> dict[str, set[str]]:
    """Return {PROCESS_NAME: {fields it sets}} for every `withName:` block.

    A selector's name group can be a `|`-separated alternation naming
    several processes (conf/modules.config:140, :270) -- every name in the
    alternation gets credited with the fields the shared block sets. A
    process named by more than one `withName:` block (e.g. GENERATE_
    PREPROCESS_QC, matched by both the shared QC-errorStrategy alternation
    at :140 and its own dedicated block at :185) has its fields unioned
    across all matching blocks.
    """
    text = MODULES_CONFIG.read_text()
    fields_by_process: dict[str, set[str]] = {}
    for match in WITH_NAME_OPEN_RE.finditer(text):
        selector = match.group(1) if match.group(1) is not None else match.group(2)
        names = selector.split("|")

        # Brace-match from the opening `{` (match.end() - 1) to find the
        # block body -- a regex cannot balance the nested `{ }` closures and
        # lists a withName block commonly contains.
        depth = 1
        pos = match.end()
        while depth > 0 and pos < len(text):
            if text[pos] == "{":
                depth += 1
            elif text[pos] == "}":
                depth -= 1
            pos += 1
        body = text[match.end() : pos - 1]

        fields = {m.group(1) for m in RESOURCE_FIELD_RE.finditer(body)}
        for name in names:
            fields_by_process.setdefault(name, set()).update(fields)
    return fields_by_process


def test_withname_brace_matcher_handles_nested_closures():
    """Regression guard on the brace-matcher itself.

    conf/modules.config's withName blocks routinely nest `{ }` inside
    `memory = { ... }` closures and `publishDir = [[...],[...]]` lists. A
    naive "first `}` closes the block" scan would truncate the body at the
    closure's own closing brace and silently under-read (or over-read, if it
    instead scanned to the last `}` in the file) the fields a block sets.
    """
    text = (
        "withName: 'FOO' {\n"
        "    memory = { 8.GB * task.attempt }\n"
        "    publishDir = [ path: { \"x\" }, mode: 'copy' ]\n"
        "}\n"
        "withName: 'BAR' {\n"
        "    cpus = 1\n"
        "}\n"
    )
    match = WITH_NAME_OPEN_RE.search(text)
    depth = 1
    pos = match.end()
    while depth > 0 and pos < len(text):
        if text[pos] == "{":
            depth += 1
        elif text[pos] == "}":
            depth -= 1
        pos += 1
    body = text[match.end() : pos - 1]
    assert "BAR" not in body
    assert {m.group(1) for m in RESOURCE_FIELD_RE.finditer(body)} == {"memory"}


def test_exactly_one_resource_owner_per_process():
    """Every process must have exactly one resource owner: a `label`, or full
    `withName:` coverage of cpus/memory/time -- never both, never neither.

    `withName:` always wins when both are present (Nextflow config-merge
    precedence), so a `label` alongside a `withName:` block that sets all
    three fields is provably dead: removing it changes nothing, and leaving
    it in place misleads a maintainer who edits the label expecting an
    effect (this is exactly what motivated this task -- see
    export_geojson.nf, which used to declare `process_medium` while its
    withName block set `memory = 32.GB`, a 3x disagreement).

    A process with neither a label nor full withName coverage is worse: the
    field(s) nothing sets fall through to conf/base.config's defaults (1
    cpu, 6 GB, 4 h) with no error, no warning, and no visible diff against a
    correctly-configured process -- until it dies on real data.
    """
    processes = find_module_processes()
    labelled = find_labelled_processes()
    withname_fields = find_withname_resource_fields()

    assert processes, "No `process NAME {` declarations found under modules/local/*.nf -- scan pattern may be stale."

    both_owners = []
    no_owner = []
    for name in sorted(processes):
        has_label = name in labelled
        fields = withname_fields.get(name, set())
        full_withname_coverage = fields == RESOURCE_FIELDS

        if has_label and full_withname_coverage:
            both_owners.append(
                f"{name} ({processes[name].relative_to(ROOT)}): has a `label` AND a "
                "`withName:` block in conf/modules.config setting cpus, memory, AND "
                "time -- the label is dead; withName always wins, so remove the label."
            )
        elif not has_label and not full_withname_coverage:
            missing = sorted(RESOURCE_FIELDS - fields)
            no_owner.append(
                f"{name} ({processes[name].relative_to(ROOT)}): has no `label` and its "
                f"`withName:` block(s) do not set {missing} -- that field silently "
                "inherits conf/base.config's defaults. Add a `label` back or set the "
                "missing field(s) explicitly in conf/modules.config."
            )

    assert not both_owners, (
        f"{len(both_owners)} process(es) have a redundant `label` fully shadowed by a "
        "`withName:` block:\n" + "\n".join(both_owners)
    )
    assert not no_owner, (
        f"{len(no_owner)} process(es) have no resource owner for one or more of "
        "cpus/memory/time:\n" + "\n".join(no_owner)
    )


# ---------------------------------------------------------------------------
# docs/resources.md's per-process table vs conf/modules.config.
#
# `docs/resources.md` restates every process's cpus/memory/time by hand. Nothing
# stopped those numbers drifting from `conf/modules.config`, and four rows were
# still wrong when this guard was written: TILED_SOLVE documented as 4 GB against
# a config that says 1 GB (conf/modules.config:340); TILED_REG_TILE and
# TILED_STITCH documented as flat 8 GB after both became parameter-derived
# closures (:317, :360); and EXPORT_SPATIALDATA described as having "no label at
# all", which cascaded into a false claim that it was the one process relying on
# conf/base.config's fallback -- its label is a ternary, so it has one either way
# (modules/local/export_spatialdata.nf:22). A fifth, PHENOTYPE, had already gone
# with the process it described. Three of the four are reproduced as watched
# failures below before this guard was trusted.
#
# This is the same shape as tests/test_layout.py: one file describes, one file
# enforces, and the guard checks both directions --
#   * every row in the doc table names a real process, and its cpus / memory /
#     time / owner agree with what conf/modules.config (or the module's label)
#     actually sets;
#   * every process that has a label or a `withName:` resource block has a row.
#
# How the numbers are compared, and why not more loosely:
#   * cpus and time are evaluated to integers on both sides. If a config
#     expression is something this guard cannot evaluate, the row FAILS with the
#     expression quoted -- it does not skip. An earlier revision guarded on
#     `if expected: ...`, so `cpus = { params.seg_gpu ? 16 : 12 }` deleted the
#     check silently and the suite stayed green with the doc saying `1`.
#   * memory is compared against COMPUTED values, never against "every integer
#     that appears in the expression". That looser rule let a wrong doc number
#     pass whenever it coincided with a tier constant or a byte-shift operand in
#     the same closure: CONVERT_IMAGE's base documented as 48 GB (a tier constant
#     two clauses away) and SPLIT_CHANNELS' `f<5` threshold documented as `f<30`
#     (the `>> 30` shift) both passed.
#   * memory is also compared PAIRWISE, not as pools of numbers. A tier ladder
#     is an ordered list of (threshold, magnitude) rungs; a size-linear request
#     is a (coefficient, addend) position pair. The pooled version let a doc cell
#     hold every right number in the wrong place and pass: SPLIT_CHANNELS
#     documented as `f<5 -> 64, f<15 -> 32` against a config saying the inverse,
#     and PREPROCESS's `7`/`8` swapped between the per-GiB coefficient and the
#     flat addend. Both now fail by name. See memory_cell_mismatch, which knows
#     exactly three shapes and refuses loudly on a fourth.
#
# What is deliberately NOT compared, and why:
#   * `TILED_REG_TILE` / `TILED_STITCH` memory is `tiledRegTileGb(params)` /
#     `tiledStitchGb(params)` -- derived from `reg_tiled_tile`/`reg_tiled_halo`
#     and `reg_tiled_out_tile` at task submission, not a literal. Re-deriving it
#     here would duplicate the very
#     formula this guard exists to stop duplicating, and evaluating Groovy from
#     pytest is not on the table. So for these two rows the guard asserts the
#     HELPER NAME appears in both the config expression and the doc cell, and
#     leaves the illustrative "4 GB at defaults" figure unchecked. Renaming or
#     replacing a helper therefore still fails the build; re-tuning the formula
#     without touching its name does not. docs/resources.md says so in the same
#     words, so the unchecked figure is labelled as such where a reader sees it.
#   * `SPLIT_PRIOR_PYRAMID` and `SEG_QC_SEGMENT` are ALIASES, not modules. Their
#     `withName:` blocks set no resource field (they override publishDir /
#     ext.prefix only), so neither is required to have a row. SEG_QC_SEGMENT has
#     one anyway -- it is a documented GPU/QC cost -- and ALIASES below maps it
#     back to SEGMENT so its numbers are still checked against a real config
#     block rather than skipped.
#   * The **ramp SHAPE** of a flat request. `32.GB * (2 ** (task.attempt - 1))`
#     documented as `32 GB x attempt` still passes: only magnitudes are compared,
#     and 32 is the attempt-1 value either way. Known, disclosed, left as-is --
#     a wrong RUNG COUNT is caught (the later rungs would not be in the computed
#     set), only the notation between them is not.
#   * Everything in docs/resources.md outside the per-process tables. The label
#     tier table, the conf/base.config fallback table, the `maxForks` column, the
#     global-ceiling and profile tables, the retry-policy tables and the
#     container table are all hand-maintained and unguarded. Only rows whose
#     table's first header cell is literally `Process` are read.
#   * Prose. A cell whose numbers are right but whose words are wrong (e.g.
#     "tier on channels + masks" naming the wrong inputs) passes.
# ---------------------------------------------------------------------------

DOCS_RESOURCES = ROOT / "docs" / "resources.md"

# Process names in docs/resources.md that are Nextflow ALIASES of another
# process, so conf/modules.config configures them under the original name.
ALIASES = {
    "SEG_QC_SEGMENT": "SEGMENT",
    # EXTRACT_CELL_PROPERTIES / EXTRACT_NUCLEI_PROPERTIES are two aliases of the
    # single modules/local/extract_properties.nf process. Both keep a doc-table
    # row because both are what an operator sees in a trace, in the size-log
    # rollup and in a `withName:` override; the row's numbers are checked against
    # EXTRACT_PROPERTIES' real config block.
    "EXTRACT_CELL_PROPERTIES": "EXTRACT_PROPERTIES",
    "EXTRACT_NUCLEI_PROPERTIES": "EXTRACT_PROPERTIES",
}

# `withLabel: 'name' { ... }` -- the opening brace is needed to brace-match the
# body, exactly as WITH_NAME_OPEN_RE does for withName blocks.
WITH_LABEL_OPEN_RE = re.compile(r"withLabel:\s*(?:'([^']+)'|\"([^\"]+)\")\s*\{")

# The start of a `cpus = ` / `memory = ` / `time = ` assignment. Same anchoring
# as RESOURCE_FIELD_RE, but this one is used to locate the right-hand side so
# the expression can be read out, not merely to note that the field is set.
FIELD_ASSIGN_RE = re.compile(r"^[ \t]*(cpus|memory|time)[ \t]*=[ \t]*", re.MULTILINE)

# Any decimal number, in a config expression or a doc table cell.
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")

# A duration written the way both conf/modules.config and the doc write it:
# `2.h`, `12.h`. Restricting the doc side to this shape keeps the CPU count and
# the tier thresholds in neighbouring cells from being read as hours.
HOURS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*\.\s*h\b")
# `time` is compared in HOURS on both sides. COMPILE_PANEL is the one process
# whose config states minutes (`30.min * task.attempt`), so a minute literal is
# normalised to hours (30.min -> 0.5) rather than being left unevaluable -- the
# guard fails loudly on anything it cannot evaluate, so an unhandled unit would
# have blocked the row instead of silently skipping it.
MINUTES_RE = re.compile(r"(\d+(?:\.\d+)?)\s*\.\s*min\b")

# `tiledRegTileGb(params)` -- a per-task value derived from params at submission
# time rather than a literal. See the header above for why these are exempt from
# the numeric comparison and what is asserted instead.
DERIVED_HELPER_RE = re.compile(r"\b(\w+)\(\s*params\s*\)")

# One rung of a size-tier ladder, CONFIG side: `file_gb < 10 ? 32.GB`. Threshold
# and magnitude are captured TOGETHER, in one match, so the pair cannot come
# apart. An earlier revision collected thresholds and magnitudes into two
# independent multisets, which meant a doc cell could state the right set of
# each and pair them backwards -- `f<5 -> 64, f<15 -> 32` against a config that
# says the opposite -- and pass. That is a reader sizing an allocation from a
# table that states the inverse of the truth.
CONFIG_TIER_RE = re.compile(r"<\s*(\d+(?:\.\d+)?)\s*\?\s*(\d+(?:\.\d+)?)\s*\.GB")

# The ladder's final `else` branch: the first `: N.GB` after its last rung.
CONFIG_ELSE_RE = re.compile(r":\s*(\d+(?:\.\d+)?)\s*\.GB")

# The one non-ladder size-dependent shape in conf/modules.config: PREPROCESS's
# `... * 7.GB * task.attempt + 8.GB`, linear in file size. The two numbers play
# DIFFERENT roles -- 7 is the per-GiB-of-input coefficient, 8 a flat addend --
# so they are captured positionally rather than pooled.
CONFIG_SIZE_LINEAR_RE = re.compile(
    r"\*\s*(\d+(?:\.\d+)?)\s*\.GB\s*\*\s*task\.attempt\s*\+\s*(\d+(?:\.\d+)?)\s*\.GB"
)

# The same three shapes as docs/resources.md writes them.
#   rung:   `f<10` -> 32       (backtick and `+` sign both optional)
#   else:   else 128 GB / else +64 GB
#   linear: `f x 7 GB x attempt + 8 GB`
DOC_TIER_RE = re.compile(
    r"f\s*<\s*(\d+(?:\.\d+)?)\s*`?\s*(?:\u2192|->)\s*\+?\s*(\d+(?:\.\d+)?)"
)
DOC_ELSE_RE = re.compile(r"else\s*\+?\s*(\d+(?:\.\d+)?)")
DOC_SIZE_LINEAR_RE = re.compile(
    r"f\s*\u00d7\s*(\d+(?:\.\d+)?)\s*GB\s*\u00d7\s*attempt\s*\+\s*(\d+(?:\.\d+)?)\s*GB"
)

# The one BATCH-SCALED time shape: `12.h + 1.h * markers.size() * task.attempt`.
# QUANTIFY quantifies a patient's whole panel in one task, so its wall must grow
# with the batch; a flat number there is a walltime kill that takes the patient's
# ENTIRE panel instead of one marker, and conf/modules.config's errorStrategy
# 'ignore' branch can drop it from a green run.
#
# The two numbers play DIFFERENT roles -- the first is a floor that no batch may
# fall below (a two-marker patient must not get less wall than the per-marker
# fan-out gave one marker), the second is the per-marker allowance -- so they are
# captured POSITIONALLY, exactly as CONFIG_SIZE_LINEAR_RE does for PREPROCESS.
# Pooling them would let a doc cell state the right pair backwards.
#
# Without this pair, `expr_values_per_attempt` returns None for the shape and the
# time check REFUSES the row outright ("not a plain number this guard can
# evaluate ... NOTHING is checking it"). That refusal was confirmed empirically
# before this was written, and it is the guard's own documented remedy: teach the
# evaluator the new shape rather than exempt or simplify.
CONFIG_TIME_BATCH_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*\.\s*h\s*\+\s*(\d+(?:\.\d+)?)\s*\.\s*h\s*\*\s*"
    r"markers\s*\.\s*size\(\)\s*\*\s*task\.attempt"
)
# The same shape as docs/resources.md writes it: `12.h + 1.h × markers × attempt`.
DOC_TIME_BATCH_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*\.?\s*h\s*\+\s*(\d+(?:\.\d+)?)\s*\.?\s*h\s*"
    r"\u00d7\s*markers\s*\u00d7\s*attempt"
)

# The doubling ramp as the doc renders it: `2^(attempt-1)` (Unicode minus or
# ASCII hyphen). Its `2` and `1` are notation, not memory magnitudes, so the
# whole token is removed before the cell's numbers are read.
DOC_RAMP_RE = re.compile(r"\d+\s*\^\s*\(\s*attempt\s*[\u2212-]\s*\d+\s*\)")

# Nextflow retries up to maxRetries = 3 (conf/base.config), so a task runs on
# attempt 1..4 and a ramp has exactly four rungs. docs/resources.md enumerates
# them for WARP_SEG_QC ("32 / 64 / 128 / 256"); computing the same four values
# from the config expression is what makes those numbers checkable.
ATTEMPTS = (1, 2, 3, 4)


def _brace_match(text: str, open_brace_index: int) -> str:
    """Return the body between `text[open_brace_index] == '{'` and its partner."""
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


def _strip_comments(text: str) -> str:
    """Drop `/* */` and `//` comments from an extracted expression.

    Load-bearing, and demonstrated in situ rather than asserted. An earlier
    revision of this docstring called it a no-op on the grounds that no
    expression in conf/modules.config contains a comment today -- true of the
    annotations that look like they would (WARP_SEG_QC's `// 32 / 64 / 128 / 256`
    at conf/modules.config:456, TILED_SOLVE's note at :337-340), which sit
    OUTSIDE the closure braces the extractor reads. But CONVERT_IMAGE, SEGMENT,
    SPLIT_CHANNELS, MERGE_AND_PYRAMID and GENERATE_REGISTRATION_QC all have
    MULTI-LINE memory closures whose interior is fair game for a comment.
    Verified: adding `// was 999.GB before the rewrite` inside CONVERT_IMAGE's
    closure, with `999 GB` in the doc cell, FAILS the doc/config comparison with
    this helper active and PASSES with it neutered to `return text` -- i.e.
    without it, a stale number parked in a config comment silently licenses the
    same stale number in the documentation.
    """
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", text)


def parse_resource_exprs(body: str) -> dict[str, str]:
    """Return {field: right-hand-side expression} for a withName/withLabel body.

    The right-hand side is either a closure (`memory = { ... }`, read by
    brace-matching, since it nests) or a bare literal to end of line
    (`cpus = 8`).
    """
    exprs: dict[str, str] = {}
    for match in FIELD_ASSIGN_RE.finditer(body):
        pos = match.end()
        if pos < len(body) and body[pos] == "{":
            expr = _brace_match(body, pos)
        else:
            end = body.find("\n", pos)
            expr = body[pos:] if end == -1 else body[pos:end]
        exprs[match.group(1)] = _strip_comments(expr).strip()
    return exprs


def find_label_resource_exprs() -> dict[str, dict[str, str]]:
    """Return {label: {field: expression}} for every `withLabel:` block."""
    text = MODULES_CONFIG.read_text()
    return {
        (m.group(1) if m.group(1) is not None else m.group(2)): parse_resource_exprs(
            _brace_match(text, m.end() - 1)
        )
        for m in WITH_LABEL_OPEN_RE.finditer(text)
    }


def find_withname_resource_exprs() -> dict[str, dict[str, str]]:
    """Return {PROCESS: {field: expression}} unioned over matching withName blocks."""
    text = MODULES_CONFIG.read_text()
    exprs: dict[str, dict[str, str]] = {}
    for match in WITH_NAME_OPEN_RE.finditer(text):
        selector = match.group(1) if match.group(1) is not None else match.group(2)
        body = _brace_match(text, match.end() - 1)
        found = parse_resource_exprs(body)
        for name in selector.split("|"):
            exprs.setdefault(name, {}).update(found)
    return exprs


def find_process_labels() -> dict[str, list[str]]:
    """Return {PROCESS: [labels it can resolve to]} for modules/local/*.nf.

    A list, not a single value: `export_spatialdata.nf`'s label is a ternary and
    can resolve to either branch depending on `--spatialdata_include_image`.
    """
    labels: dict[str, list[str]] = {}
    for name, path in find_module_processes().items():
        found: list[str] = []
        for line in path.read_text().splitlines():
            if LABEL_LINE_RE.match(line):
                found.extend(extract_labels_from_line(line))
        labels[name] = found
    return labels


def _eval_arithmetic(expr: str) -> float | None:
    """Evaluate a pure-arithmetic expression, or return None if it is not one.

    Only numbers and `+ - * / ** ( )` are honoured. Anything else -- a ternary,
    a `.size()` call, a `?:` -- returns None, which is how a size-dependent tier
    is distinguished from a flat request. Uses `ast` rather than `eval` so a
    stray identifier cannot be executed.
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError:
        return None

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            operand = walk(node.operand)
            return None if operand is None else (operand if isinstance(node.op, ast.UAdd) else -operand)
        if isinstance(node, ast.BinOp):
            left, right = walk(node.left), walk(node.right)
            if left is None or right is None:
                return None
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            if isinstance(node.op, ast.Pow):
                return left**right
        return None

    return walk(tree)


def expr_values_per_attempt(expr: str) -> list[float] | None:
    """Return the expression's value on attempts 1..4, or None if not a constant.

    `12.GB * task.attempt` -> [12, 24, 36, 48]; `100.GB + 100.GB * task.attempt`
    -> [200, 300, 400, 500]; `32.GB * (2 ** (task.attempt - 1))` ->
    [32, 64, 128, 256]. A tiered closure (`(f < 10 ? 32.GB : ...)`) or a
    param-derived one returns None -- those are compared differently.
    """
    if DERIVED_HELPER_RE.search(expr):
        return None
    values = []
    for attempt in ATTEMPTS:
        arithmetic = (
            MINUTES_RE.sub(
                lambda m: repr(float(m.group(1)) / 60.0),
                expr.replace("task.attempt", str(attempt)),
            )
            .replace(".GB", "")
            .replace(".h", "")
        )
        value = _eval_arithmetic(arithmetic)
        if value is None:
            return None
        values.append(value)
    return values


def _numbers(text: str) -> set[float]:
    return {float(m.group(0)) for m in NUMBER_RE.finditer(text)}


def _gb_literal_counts(expr: str) -> Counter:
    """Multiset of the `N.GB` magnitudes a config expression names.

    A multiset, not a set: CONVERT_IMAGE's closure names `24.GB` twice (once as
    the per-attempt base, once as a tier constant) and collapsing them would let
    the doc drop one of the two and still pass.
    """
    return Counter(
        float(m.group(1)) for m in re.finditer(r"(\d+(?:\.\d+)?)\s*\.GB", expr)
    )


def _doc_magnitude_counts(cell: str) -> Counter:
    """Multiset of the GB magnitudes a doc memory cell states, for a FLAT row.

    The doubling-ramp notation (`2^(attempt-1)`) is removed first: its `2` and
    `1` are structure, not magnitudes. Tiered and size-linear rows do not use
    this -- they are compared as (threshold, magnitude) pairs and as positional
    roles respectively, which is the whole point of _ladder/_size_linear below.
    """
    stripped = DOC_RAMP_RE.sub(" ", cell)
    return Counter(float(m.group(0)) for m in NUMBER_RE.finditer(stripped))


def _config_ladder(expr: str):
    """Return (rungs, else_gb, leftover_gb) for a size-tier ladder, or None.

    `rungs` is an ORDERED list of (threshold, GB) pairs read straight off the
    ternary chain, so `file_gb < 5 ? 32.GB : file_gb < 15 ? 64.GB : 128.GB`
    gives [(5, 32), (15, 64)] with else_gb 128. `leftover_gb` is the multiset of
    `N.GB` literals outside the ladder -- CONVERT_IMAGE's `+ 24.GB *
    task.attempt` per-attempt base is the only one today.
    """
    rungs = [(float(m.group(1)), float(m.group(2))) for m in CONFIG_TIER_RE.finditer(expr)]
    if not rungs:
        return None
    last = list(CONFIG_TIER_RE.finditer(expr))[-1]
    else_match = CONFIG_ELSE_RE.search(expr, last.end())
    else_gb = float(else_match.group(1)) if else_match else None
    consumed = Counter(gb for _, gb in rungs)
    if else_gb is not None:
        consumed[else_gb] += 1
    return rungs, else_gb, _gb_literal_counts(expr) - consumed


def _doc_ladder(cell: str):
    """Return (rungs, else_gb, leftover_gb) as docs/resources.md states them."""
    rungs = [(float(m.group(1)), float(m.group(2))) for m in DOC_TIER_RE.finditer(cell)]
    else_match = DOC_ELSE_RE.search(cell)
    else_gb = float(else_match.group(1)) if else_match else None
    leftover_text = DOC_TIER_RE.sub(" ", DOC_RAMP_RE.sub(" ", cell))
    if else_match:
        leftover_text = leftover_text.replace(else_match.group(0), " ", 1)
    leftover = Counter(float(m.group(0)) for m in NUMBER_RE.finditer(leftover_text))
    return rungs, else_gb, leftover


def _config_size_linear(expr: str):
    """Return (coefficient, addend, leftover_gb) for `* A.GB * attempt + B.GB`, or None."""
    match = CONFIG_SIZE_LINEAR_RE.search(expr)
    if not match:
        return None
    coefficient, addend = float(match.group(1)), float(match.group(2))
    return coefficient, addend, _gb_literal_counts(expr) - Counter([coefficient, addend])


def _doc_size_linear(cell: str):
    """Return (coefficient, addend, leftover_gb) as docs/resources.md states it, or None."""
    match = DOC_SIZE_LINEAR_RE.search(cell)
    if not match:
        return None
    coefficient, addend = float(match.group(1)), float(match.group(2))
    leftover_text = cell.replace(match.group(0), " ", 1)
    leftover = Counter(float(m.group(0)) for m in NUMBER_RE.finditer(leftover_text))
    return coefficient, addend, leftover


def batch_time_mismatch(expr: str, cell: str) -> str | None:
    """Why `cell` misstates a BATCH-SCALED time `expr`, or None if it states it.

    Only called for expressions matching CONFIG_TIME_BATCH_RE. The floor and the
    per-marker allowance are compared in their own positions, and any leftover
    number in the doc cell is an error rather than noise -- a stray magnitude is
    how a cell states two rules and is checked against one.
    """
    config = CONFIG_TIME_BATCH_RE.search(expr)
    assert config, f"batch_time_mismatch called on a non-batch expression: {expr!r}"
    documented = DOC_TIME_BATCH_RE.search(cell)
    if not documented:
        return (
            f"does not state the batch-scaled shape at all. conf/modules.config "
            f"says `{config.group(0)}`; the doc must say "
            f"`{config.group(1)}.h + {config.group(2)}.h \u00d7 markers \u00d7 attempt`. "
            "A cell that drops the `\u00d7 markers` reads as a flat wall, which is "
            "the exact defect the scaling exists to remove."
        )
    floor, per_marker = float(config.group(1)), float(config.group(2))
    doc_floor, doc_per = float(documented.group(1)), float(documented.group(2))
    if (doc_floor, doc_per) != (floor, per_marker):
        return (
            f"states floor {doc_floor} h and {doc_per} h per marker, but "
            f"conf/modules.config gives {floor} h and {per_marker} h per marker"
        )
    leftover = _numbers(cell.replace(documented.group(0), " ", 1))
    if leftover:
        return (
            f"states {sorted(leftover)} outside the batch-scaled shape -- every "
            "magnitude in the cell must belong to a rule the config actually has"
        )
    return None


def memory_cell_mismatch(expr: str, cell: str) -> str | None:
    """Return why `cell` misstates `expr`, or None if it states it correctly.

    Three shapes, because conf/modules.config has exactly three. Anything else
    fails loudly rather than falling through to a looser comparison -- the
    fall-through is what produced every hole found in review so far.

    * a **flat** request (`32.GB * task.attempt`, `100.GB + 100.GB *
      task.attempt`, `32.GB * (2 ** (task.attempt - 1))`) is evaluated on
      attempts 1..4; every magnitude in the doc cell must be one of those four
      values, and the attempt-1 value must be among them. Comparing against the
      COMPUTED values -- not against every integer that appears in the
      expression -- is what stops a tier constant or a `>> 30` byte shift from
      laundering a wrong number.
    * a **size-tier ladder** (`(file_gb < 10 ? 32.GB : ...) * task.attempt`)
      cannot be evaluated without a file, so its rungs are compared as an
      ORDERED list of (threshold, magnitude) PAIRS, plus the `else` branch, plus
      a multiset of any `N.GB` outside the ladder. Pairs, not two independent
      multisets: with the latter, a doc cell stating the right thresholds and
      the right magnitudes but pairing them backwards -- `f<5 -> 64,
      f<15 -> 32` against a config that says `f<5 -> 32, f<15 -> 64` -- passed.
    * a **size-linear** request (PREPROCESS's `* 7.GB * task.attempt + 8.GB`)
      has no `<` at all, so it is not a ladder; its two numbers are compared
      POSITIONALLY, coefficient against coefficient and addend against addend.
      Pooling them let `7` and `8` be swapped in the doc and still pass, even
      though they mean different things. Given its own shape rather than a loud
      refusal because the shape is genuinely checkable and a refusal would need
      an exemption that checks nothing.
    """
    values = expr_values_per_attempt(expr)
    if values is not None:
        documented = _doc_magnitude_counts(cell)
        if not documented:
            return f"states no GB value at all against a flat request of {values[0]} GB"
        extra = sorted(set(documented) - set(values))
        if extra:
            return (
                f"states {extra} GB, which is not a value `{expr}` takes on any "
                f"attempt (it gives {values})"
            )
        if values[0] not in documented:
            return f"never states the attempt-1 value {values[0]} GB of `{expr}`"
        return None

    ladder = _config_ladder(expr)
    if ladder is not None:
        config_rungs, config_else, config_extra = ladder
        doc_rungs, doc_else, doc_extra = _doc_ladder(cell)
        if doc_rungs != config_rungs:
            mispaired = [
                f"f<{int(th) if th.is_integer() else th} states "
                f"{int(gb) if gb.is_integer() else gb} GB"
                for th, gb in doc_rungs
                if (th, gb) not in config_rungs
            ]
            return (
                f"tiers as {doc_rungs} but `{expr}` tiers as {config_rungs}"
                + (f" -- mispaired: {mispaired}" if mispaired else "")
            )
        if doc_else != config_else:
            return f"gives the else branch {doc_else} GB but `{expr}` gives {config_else}"
        if doc_extra != config_extra:
            return (
                f"states {sorted(doc_extra.elements())} GB outside the tier ladder "
                f"but `{expr}` has {sorted(config_extra.elements())}"
            )
        return None

    linear = _config_size_linear(expr)
    if linear is not None:
        config_coefficient, config_addend, config_extra = linear
        documented = _doc_size_linear(cell)
        if documented is None:
            return (
                f"does not state `{expr}`'s size-linear shape "
                f"(`f x {config_coefficient} GB x attempt + {config_addend} GB`)"
            )
        doc_coefficient, doc_addend, doc_extra = documented
        if (doc_coefficient, doc_addend) != (config_coefficient, config_addend):
            return (
                f"states coefficient {doc_coefficient} GB/GiB and addend "
                f"{doc_addend} GB, but `{expr}` has coefficient "
                f"{config_coefficient} GB/GiB and addend {config_addend} GB"
            )
        if doc_extra != config_extra:
            return (
                f"states {sorted(doc_extra.elements())} GB outside the size-linear "
                f"term but `{expr}` has {sorted(config_extra.elements())}"
            )
        return None

    return (
        f"cannot be checked: `{expr}` is none of the three shapes this guard "
        "knows -- a flat request it can evaluate, a `< N ? M.GB` tier ladder, or "
        "a `* A.GB * task.attempt + B.GB` size-linear term. Teach "
        "memory_cell_mismatch the new shape or exempt the row explicitly; do not "
        "leave the field silently unchecked"
    )


def parse_doc_process_table() -> dict[str, dict[str, str]]:
    """Return {PROCESS: {column: cell}} for every per-process row in docs/resources.md.

    Only tables whose first header cell is literally `Process` are read, which
    excludes the label table (`Label`), the fallback table (`Resource`), the
    ceiling tables and the container table. Column lookup is by header name, so
    the tiled table's extra `maxForks` column does not shift the others.
    """
    lines = DOCS_RESOURCES.read_text().splitlines()
    rows: dict[str, dict[str, str]] = {}
    headers: list[str] | None = None
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            headers = None
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if set(stripped) <= set("|- "):  # the `|---|---|` separator row
            continue
        normalised = [
            (c.replace("`", "").replace("*", "").split() or [""])[0].lower() for c in cells
        ]
        if normalised and normalised[0] == "process":
            headers = normalised
            continue
        if headers is None:
            continue
        name = cells[0].strip("`")
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            continue
        rows[name] = dict(zip(headers, cells))
    return rows


def resource_owner_case(name: str) -> str:
    """Return 'withName', 'label' or 'partial' for a module process.

    Mirrors the three cases docs/resources.md's `<div class="gate">` names, and
    is what the per-row Owner column and the three case counts are checked
    against.
    """
    fields = find_withname_resource_fields().get(name, set())
    if not find_process_labels().get(name):
        return "withName"
    return "label" if not fields else "partial"


def effective_exprs(name: str, field: str) -> list[str]:
    """Return the expression(s) that can supply `field` for `name`.

    One entry normally; two when the module's `label` is a ternary and the field
    is label-owned, since either branch is reachable at runtime.
    """
    base = ALIASES.get(name, name)
    withname = find_withname_resource_exprs()
    expr = withname.get(name, {}).get(field) or withname.get(base, {}).get(field)
    if expr:
        return [expr]
    label_exprs = find_label_resource_exprs()
    return [
        label_exprs[label][field]
        for label in find_process_labels().get(base, [])
        if field in label_exprs.get(label, {})
    ]


def test_strip_comments_removes_numbers_a_maintainer_would_annotate_with():
    """A number inside a comment in a memory closure must not count as config.

    conf/modules.config has no such comment today, so this is the only place the
    branch is exercised in the checked-in tree -- but it is not hypothetical:
    see _strip_comments for the in-situ demonstration that adding one to
    CONVERT_IMAGE's closure lets a stale doc number pass without this helper.
    """
    annotated = "32.GB * task.attempt   // was 64.GB before the bounding-box rewrite"
    assert _strip_comments(annotated).strip() == "32.GB * task.attempt"
    assert 64.0 not in _numbers(_strip_comments(annotated))
    assert _strip_comments("1.GB /* 4.GB was 120x the observed peak */ * task.attempt").split() == [
        "1.GB",
        "*",
        "task.attempt",
    ]


def test_doc_table_parser_finds_the_per_process_rows_only():
    """Regression guard on the doc-table parser itself.

    It must pick up the per-process tables and skip the label table -- whose
    rows (`process_single`, ...) are lower-case and whose first header cell is
    `Label`, not `Process`. A parser that swallowed those would compare a label
    against conf/modules.config as if it were a process and could never fail,
    and one that mis-indexed columns would compare cpus against memory.
    """
    rows = parse_doc_process_table()
    assert "SEGMENT" in rows, "per-process tables not parsed -- scan pattern may be stale"
    assert "process_single" not in rows, "the `Label` table must not be read as processes"
    assert rows["SEGMENT"]["cpus"] == "`8`"
    assert rows["SEGMENT"]["time"] == "`4.h × attempt`"
    assert rows["SEGMENT"]["owner"] == "`withName`"
    # The tiled table carries an extra trailing column; lookup is by header
    # name, so `time` must still be the time and not shift into `maxForks`.
    assert rows["TILED_SOLVE"]["time"] == "`8.h × attempt` *(label)*"
    assert rows["TILED_SOLVE"]["maxforks"] == "—"


def test_expr_values_per_attempt_handles_the_shapes_in_modules_config():
    """Regression guard on the evaluator.

    It must fold the two ramp shapes conf/modules.config uses (linear and
    doubling) and the `A + B * attempt` shape of the medium/high labels, and it
    must REFUSE a tiered or param-derived closure rather than silently returning
    a partial number that would then be compared as if it were the whole rule.
    """
    assert expr_values_per_attempt("12.GB * task.attempt") == [12, 24, 36, 48]
    assert expr_values_per_attempt("100.GB + 100.GB * task.attempt") == [200, 300, 400, 500]
    assert expr_values_per_attempt("32.GB * (2 ** (task.attempt - 1))") == [32, 64, 128, 256]
    assert expr_values_per_attempt("8") == [8, 8, 8, 8]
    assert expr_values_per_attempt("(Math.ceil(tiledRegTileGb(params)) as int).GB * task.attempt") is None
    assert expr_values_per_attempt("(file_gb < 10 ? 32.GB : 64.GB) * task.attempt") is None


def test_batch_time_mismatch_binds_each_number_to_its_role():
    """The floor and the per-marker allowance must not be interchangeable.

    QUANTIFY's wall is `12.h + 1.h * markers.size() * task.attempt`: a floor no
    batch may fall below, plus an allowance per marker in the batch. A doc cell
    stating the same two numbers in the other order describes a WILDLY different
    rule (12 h per marker on top of a 1 h floor), so the two are compared in
    their own positions. Every branch below was watched failing.
    """
    expr = "12.h + 1.h * markers.size() * task.attempt"
    good = "`12.h + 1.h \u00d7 markers \u00d7 attempt`"
    assert batch_time_mismatch(expr, good) is None

    # swapped roles -- the failure a pooled comparison cannot see
    assert "states floor 1.0 h and 12.0 h per marker" in (
        batch_time_mismatch(expr, "`1.h + 12.h \u00d7 markers \u00d7 attempt`") or ""
    )
    # the `\u00d7 markers` dropped: reads as a flat wall, the defect the scaling removes
    assert "does not state the batch-scaled shape at all" in (
        batch_time_mismatch(expr, "`13.h \u00d7 attempt`") or ""
    )
    # a magnitude belonging to no rule the config has
    assert "outside the batch-scaled shape" in (
        batch_time_mismatch(expr, good + " (max 40)") or ""
    )
    # and the evaluator must still REFUSE the raw shape, so this comparator is
    # the only thing that can accept it -- if expr_values_per_attempt ever folded
    # it to a number, the doc cell would be compared against a fiction.
    assert expr_values_per_attempt(expr) is None


def test_memory_cell_mismatch_compares_pairs_not_pooled_numbers():
    """The tiered and size-linear paths must bind each number to its role.

    Every assertion here is a shape that PASSED an earlier revision: the
    comparison pooled thresholds and magnitudes into two independent multisets,
    so a doc cell could hold the right numbers in the wrong places. The last two
    cases are the other direction -- duplicate magnitudes and duplicate
    thresholds are legitimate config, and a pairing comparison is exactly where
    they would false-positive.
    """
    ladder = "(file_gb < 5 ? 32.GB : file_gb < 15 ? 64.GB : 128.GB) * task.attempt"
    correct = "tier: `f<5` \u2192 32, `f<15` \u2192 64, else 128 GB, `\u00d7 attempt`"
    swapped = "tier: `f<5` \u2192 64, `f<15` \u2192 32, else 128 GB, `\u00d7 attempt`"
    assert memory_cell_mismatch(ladder, correct) is None
    assert "mispaired" in (memory_cell_mismatch(ladder, swapped) or "")

    linear = "(ome_tiff.size() >> 30 ?: 1) * 7.GB * task.attempt + 8.GB"
    assert memory_cell_mismatch(linear, "`f \u00d7 7 GB \u00d7 attempt + 8 GB`") is None
    assert "coefficient" in (
        memory_cell_mismatch(linear, "`f \u00d7 8 GB \u00d7 attempt + 7 GB`") or ""
    )

    # Anything that is none of the three known shapes must refuse loudly rather
    # than fall through to a looser comparison.
    assert "cannot be checked" in (
        memory_cell_mismatch("Runtime.getRuntime().maxMemory()", "`64 GB`") or ""
    )

    # Legitimate shapes a pairing comparison could wrongly reject:
    # two tiers sharing one magnitude, and two tiers sharing one threshold.
    same_gb = "(file_gb < 10 ? 32.GB : file_gb < 30 ? 32.GB : 128.GB) * task.attempt"
    assert memory_cell_mismatch(
        same_gb, "tier: `f<10` \u2192 32, `f<30` \u2192 32, else 128 GB, `\u00d7 attempt`"
    ) is None
    same_threshold = "(a < 10 ? 32.GB : b < 10 ? 64.GB : 128.GB) * task.attempt"
    assert memory_cell_mismatch(
        same_threshold,
        "tier: `f<10` \u2192 32, `f<10` \u2192 64, else 128 GB, `\u00d7 attempt`",
    ) is None


def test_every_documented_process_matches_conf_modules_config():
    """Direction 1: every row in docs/resources.md agrees with the config.

    cpus and time are compared as numbers. Memory is compared as the set of
    magnitudes the config expression can produce (its literals plus its value on
    attempts 1..4), with the attempt-1 value required to appear for a flat
    request and every `N.GB` literal required to appear for a size-tiered one.
    The two param-derived rows are matched on helper NAME instead -- see the
    header of this section.
    """
    doc_rows = parse_doc_process_table()
    processes = find_module_processes()
    assert doc_rows, "No per-process rows parsed from docs/resources.md -- pattern may be stale."

    problems: list[str] = []
    for name in sorted(doc_rows):
        row = doc_rows[name]
        base = ALIASES.get(name, name)
        if base not in processes:
            problems.append(
                f"{name}: documented in docs/resources.md but no `process {base} {{` "
                "exists under modules/local/ -- delete the row or add the alias to "
                "ALIASES in this test."
            )
            continue

        # --- cpus and time -------------------------------------------------
        # Both are plain integers everywhere in conf/modules.config today, so
        # both are compared the same way -- and both FAIL LOUDLY rather than
        # skipping when the expression is something this guard cannot evaluate.
        # An earlier revision guarded on `if expected: ...`, which meant one
        # config edit (`cpus = { params.seg_gpu ? 16 : 12 }`) silently deleted
        # the check for that field with no message at all. That is this repo's
        # signature defect -- a guard that passes because it stopped looking.
        for field, documented in (
            ("cpus", _numbers(row.get("cpus", ""))),
            ("time", {float(m.group(1)) for m in HOURS_RE.finditer(row.get("time", ""))}
                     | {float(m.group(1)) / 60.0 for m in MINUTES_RE.finditer(row.get("time", ""))}),
        ):
            exprs = effective_exprs(name, field)
            if not exprs:
                problems.append(
                    f"{name}: nothing in conf/modules.config sets `{field}` -- neither a "
                    "`withName:` block nor the module's label. The doc row cannot be "
                    "checked and the process would run on conf/base.config's defaults."
                )
                continue
            # The batch-scaled time shape is not a plain number and never will be:
            # its value depends on how many markers the task was handed. It is
            # compared as (floor, per-marker) magnitudes instead -- checked, not
            # skipped. Anything mixing the two shapes across a ternary label is
            # refused rather than half-checked.
            batch = [e for e in exprs if CONFIG_TIME_BATCH_RE.search(e)]
            if batch:
                if len(batch) != len(exprs):
                    problems.append(
                        f"{name}: `{field}` is batch-scaled on one branch and a plain "
                        f"number on another ({exprs}); the doc cell can only state one."
                    )
                    continue
                reasons = [
                    r for r in (batch_time_mismatch(e, row.get(field, "")) for e in batch) if r
                ]
                if reasons:
                    problems.append(
                        f"{name}: doc table's {field} cell {row.get(field, '')!r} {reasons[0]}"
                    )
                continue
            expected = set()
            unevaluable = [e for e in exprs if expr_values_per_attempt(e) is None]
            if unevaluable:
                problems.append(
                    f"{name}: conf/modules.config's `{field}` is not a plain number this "
                    f"guard can evaluate: {unevaluable}. The doc row states "
                    f"{sorted(documented)} and NOTHING is checking it. Simplify the "
                    "config expression, or teach expr_values_per_attempt the new shape, "
                    "or exempt the row explicitly the way the derived memory closures "
                    "are -- silently skipping the field is not an option."
                )
                continue
            for expr in exprs:
                expected.add(expr_values_per_attempt(expr)[0])
            if not (documented and documented <= expected):
                unit = "" if field == "cpus" else " h"
                problems.append(
                    f"{name}: doc table says {field} {sorted(documented)}{unit} but "
                    f"conf/modules.config gives {sorted(expected)}{unit}"
                )

        # --- memory --------------------------------------------------------
        exprs = effective_exprs(name, "memory")
        cell = row.get("memory", "")
        helpers = {m.group(1) for expr in exprs for m in DERIVED_HELPER_RE.finditer(expr)}
        if helpers:
            missing = sorted(h for h in helpers if h not in cell)
            if missing:
                problems.append(
                    f"{name}: memory is derived from {missing} in conf/modules.config, "
                    "but the doc cell does not name the helper -- a doc row stating a "
                    f"flat number here would be wrong the moment a parameter changes: {cell!r}"
                )
        elif not exprs:
            problems.append(
                f"{name}: nothing in conf/modules.config sets `memory` -- neither a "
                "`withName:` block nor the module's label."
            )
        else:
            # One expression normally; two when the module's label is a ternary,
            # in which case either branch is a legitimate thing to document.
            reasons = [memory_cell_mismatch(expr, cell) for expr in exprs]
            if all(reasons):
                # More than one reason only when the module's label is a ternary
                # and neither branch matches -- then say so for both branches
                # rather than picking one arbitrarily.
                joined = "; also ".join(dict.fromkeys(r for r in reasons if r))
                problems.append(f"{name}: doc table's memory cell {cell!r} {joined}")

        # --- owner ---------------------------------------------------------
        case = resource_owner_case(base)
        owner_cell = row.get("owner", "")
        if case == "withName":
            ok = "withName" in owner_cell
        elif case == "partial":
            ok = "partial" in owner_cell
        else:
            ok = any(label in owner_cell for label in find_process_labels()[base])
        if not ok:
            problems.append(
                f"{name}: doc table's Owner column says {owner_cell!r}, but "
                f"conf/modules.config makes it a '{case}' process"
            )

    assert not problems, (
        f"{len(problems)} row(s) in docs/resources.md disagree with "
        "conf/modules.config. conf/modules.config is the source of truth; fix "
        "the doc:\n" + "\n".join(problems)
    )


def test_every_configured_process_is_documented():
    """Direction 2: every process with a resource owner has a doc-table row.

    A process added to modules/local/ with a label or a `withName:` resource
    block but no row is invisible to anyone sizing a cluster allocation from
    docs/resources.md -- the table silently under-reports what the run will ask
    the scheduler for.
    """
    documented = {ALIASES.get(n, n) for n in parse_doc_process_table()}
    labels = find_process_labels()
    withname_fields = find_withname_resource_fields()

    missing = sorted(
        name
        for name in find_module_processes()
        if (labels.get(name) or withname_fields.get(name)) and name not in documented
    )
    assert not missing, (
        f"{len(missing)} process(es) have a resource owner in conf/modules.config "
        "but no row in docs/resources.md's per-process table -- add one under the "
        f"matching section: {missing}"
    )


def test_one_owner_rule_case_counts_match_conf_modules_config():
    """The three counts in docs/resources.md's `case 1/2/3` panel must be real.

    They were 14 / 6 / 7 against a tree holding 12 / 6 / 6 -- stale since the
    phenotyping processes were removed, and wrong about EXPORT_SPATIALDATA,
    which was counted as having no label at all.
    """
    counts = {"withName": 0, "label": 0, "partial": 0}
    for name in find_module_processes():
        counts[resource_owner_case(name)] += 1

    text = DOCS_RESOURCES.read_text()
    documented = [
        int(m.group(1))
        for m in re.finditer(r'<div class="d">[^<]*?(\d+) processes\.</div>', text)
    ]
    assert len(documented) == 3, (
        "Expected three `N processes.` counts in the one-owner-rule panel of "
        f"docs/resources.md, found {len(documented)} -- scan pattern may be stale."
    )
    assert documented == [counts["withName"], counts["label"], counts["partial"]], (
        f"docs/resources.md's one-owner-rule panel claims {documented} processes "
        f"in cases 1/2/3, but conf/modules.config + modules/local/*.nf give "
        f"{[counts['withName'], counts['label'], counts['partial']]}."
    )
