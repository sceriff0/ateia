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
# ---------------------------------------------------------------------------

DOCS_RESOURCES = ROOT / "docs" / "resources.md"

# Process names in docs/resources.md that are Nextflow ALIASES of another
# process, so conf/modules.config configures them under the original name.
ALIASES = {"SEG_QC_SEGMENT": "SEGMENT"}

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

# `tiledRegTileGb(params)` -- a per-task value derived from params at submission
# time rather than a literal. See the header above for why these are exempt from
# the numeric comparison and what is asserted instead.
DERIVED_HELPER_RE = re.compile(r"\b(\w+)\(\s*params\s*\)")

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

    Not load-bearing today, and said so rather than implied: no `cpus`/`memory`/
    `time` right-hand side in conf/modules.config currently contains a comment.
    The annotations that look like they would (WARP_SEG_QC's
    `// 32 / 64 / 128 / 256` at conf/modules.config:456, TILED_SOLVE's note about
    the 4 GB it used to ask for at :337-340) sit OUTSIDE the closure braces the
    extractor reads, so they never reach here.

    It stays because CONVERT_IMAGE, SEGMENT, SPLIT_CHANNELS, MERGE_AND_PYRAMID
    and GENERATE_REGISTRATION_QC all have multi-line memory closures, and a
    `// was 64.GB` added inside one would otherwise be read as a config value and
    silently license that stale number in the doc. test_strip_comments_removes
    _numbers_a_maintainer_would_annotate_with exercises it directly, so it is not
    untested code waiting to be wrong.
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
            expr.replace("task.attempt", str(attempt))
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


def _gb_literals(expr: str) -> set[float]:
    return {float(m.group(1)) for m in re.finditer(r"(\d+(?:\.\d+)?)\s*\.GB", expr)}


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

    conf/modules.config has no such comment today (see _strip_comments), so this
    is the only place the branch is exercised -- without it the helper would be
    dead code that could rot unnoticed.
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

        # --- cpus ----------------------------------------------------------
        exprs = effective_exprs(name, "cpus")
        expected_cpus = set()
        for expr in exprs:
            values = expr_values_per_attempt(expr)
            if values:
                expected_cpus.add(values[0])
        documented_cpus = _numbers(row.get("cpus", ""))
        if expected_cpus and not (documented_cpus and documented_cpus <= expected_cpus):
            problems.append(
                f"{name}: doc table says cpus {sorted(documented_cpus)} but "
                f"conf/modules.config gives {sorted(expected_cpus)}"
            )

        # --- time ----------------------------------------------------------
        exprs = effective_exprs(name, "time")
        expected_time = set()
        for expr in exprs:
            values = expr_values_per_attempt(expr)
            if values:
                expected_time.add(values[0])
        documented_time = {float(m.group(1)) for m in HOURS_RE.finditer(row.get("time", ""))}
        if expected_time and not (documented_time and documented_time <= expected_time):
            problems.append(
                f"{name}: doc table says time {sorted(documented_time)} h but "
                f"conf/modules.config gives {sorted(expected_time)} h"
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
        elif exprs:
            allowed: set[float] = set()
            required_options: list[set[float]] = []
            for expr in exprs:
                allowed |= _numbers(expr)
                values = expr_values_per_attempt(expr)
                if values:
                    allowed |= set(values)
                    required_options.append({values[0]})
                else:
                    required_options.append(_gb_literals(expr))
            documented_memory = _numbers(cell)
            unjustified = sorted(documented_memory - allowed)
            if unjustified:
                problems.append(
                    f"{name}: doc table's memory cell states {unjustified} GB, which "
                    "appears nowhere in the conf/modules.config expression "
                    f"{exprs!r} nor in its attempt-1..4 ramp"
                )
            if required_options and not any(opt <= documented_memory for opt in required_options):
                problems.append(
                    f"{name}: conf/modules.config asks for "
                    f"{[sorted(o) for o in required_options]} GB but the doc table's "
                    f"memory cell does not state it: {cell!r}"
                )

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
