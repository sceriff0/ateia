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

import re
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
    """
    return [m.group(1) if m.group(1) is not None else m.group(2) for m in QUOTED_STRING_RE.finditer(line)]


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
