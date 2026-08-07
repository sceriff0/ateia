#!/usr/bin/env python3
"""Assert nextflow_schema.json's start/stop enums agree with ParamUtils.STEPS.

"What is a step?" has one source of truth: `ParamUtils.STEPS` in
`lib/ParamUtils.groovy`. `STEP_ORDER`, `requiredColumnsForStep`,
`entryColumnForStep`, and `final_qc.nf`'s `KNOWN_ARTIFACT_KINDS` are all
derived from that table in Groovy, so a Groovy-side drift is structurally
impossible. `nextflow_schema.json`'s `start`/`stop` enums cannot be derived
the same way -- there is no generation step in this repo, and adding a
build-time codegen step for two enum arrays would be out of proportion to
the problem. Instead, this test is the check: it parses STEPS' `name` values
straight out of the Groovy source (the same regex-on-source-text approach
`check_param_consistency.py` uses for nextflow.config) and compares them
against the schema.

What this catches: a fourth step is added to STEPS but nextflow_schema.json's
enums are not updated (or vice versa) -- --start/--stop would then either
silently reject a valid step name or silently accept an invalid one, and
nf-schema's validateParameters() would not catch it because it has no
opinion on what a "valid" step is beyond what the enum says.

A second, independent check below closes a different gap: `lib/CsvUtils.groovy`
used to carry its own copy of the step -> entry-column mapping (a `pathColumn`
map literal in `validateInputSemantics`, identical in shape to STEPS'
`entryColumn` field) that this file's first version did not catch, because it
only compared the schema against STEPS -- a second Groovy-side table drifting
from STEPS was invisible to it. `CsvUtils.groovy` now calls
`ParamUtils.entryColumnForStep` directly instead of restating the mapping, so
there is no separate resolved value left to compare against STEPS (they are
the same function call) -- a "call it and compare" test would be vacuously
true forever and would not catch a *future* reintroduction of a parallel
map. Instead, `test_no_parallel_entry_column_mapping_outside_param_utils`
statically scans the Groovy/Nextflow source (the same source-text-regex
technique used above and in `check_param_consistency.py`) for any file other
than `lib/ParamUtils.groovy` that quotes two or more of STEPS' `entryColumn`
values together -- the textual signature of a parallel step->column table,
regardless of what shape it takes (map literal, switch, if-chain).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PARAM_UTILS_PATH = ROOT / "lib" / "ParamUtils.groovy"
SCHEMA_PATH = ROOT / "nextflow_schema.json"

STEPS_BLOCK_RE = re.compile(r"static final List STEPS = \[(.*?)\n    \]\n", re.S)
STEP_NAME_RE = re.compile(r"name\s*:\s*'([^']*)'")
ENTRY_COLUMN_RE = re.compile(r"entryColumn\s*:\s*'([^']*)'")

# One match per step entry: its name, its requiredColumns list (as raw quoted
# strings), and its entryColumn. Non-greedy up to the first ']' after each field
# is safe here because requiredColumns is a flat string list (no nested brackets)
# and entryColumn is a single quoted scalar, so the first ']' following
# requiredColumns really is that list's own close bracket.
STEP_ENTRY_RE = re.compile(
    r"name\s*:\s*'(?P<name>[^']*)'.*?"
    r"requiredColumns\s*:\s*\[(?P<cols>[^\]]*)\].*?"
    r"entryColumn\s*:\s*'(?P<entry>[^']*)'",
    re.S,
)

# Source globs scanned for a parallel step->column mapping. Groovy libs +
# every Nextflow file that could plausibly branch on a step name.
SCANNED_GLOBS = ["lib/**/*.groovy", "workflows/**/*.nf", "subworkflows/**/*.nf"]


def _steps_block_text() -> str:
    text = PARAM_UTILS_PATH.read_text()
    m = STEPS_BLOCK_RE.search(text)
    if m is None:
        raise ValueError(
            "Could not locate `static final List STEPS = [ ... ]` in "
            f"{PARAM_UTILS_PATH}. Did the table get renamed or reshaped?"
        )
    return m.group(1)


def extract_step_names() -> list[str]:
    """The ordered `name` values inside ParamUtils.STEPS, parsed from source."""
    return STEP_NAME_RE.findall(_steps_block_text())


def extract_entry_columns() -> list[str]:
    """The ordered `entryColumn` values inside ParamUtils.STEPS, parsed from source."""
    return ENTRY_COLUMN_RE.findall(_steps_block_text())


def extract_step_entries() -> list[dict]:
    """Each STEPS row as {name, requiredColumns, entryColumn}, parsed from source."""
    entries = []
    for m in STEP_ENTRY_RE.finditer(_steps_block_text()):
        cols = [c.strip().strip("'") for c in m.group("cols").split(",") if c.strip()]
        entries.append(
            {
                "name": m.group("name"),
                "requiredColumns": cols,
                "entryColumn": m.group("entry"),
            }
        )
    return entries


def extract_schema_enum(schema: dict, prop: str) -> list:
    """The raw enum list for schema property `prop`, wherever it lives."""
    props: dict[str, dict] = dict(schema.get("properties", {}))
    for group in {**schema.get("definitions", {}), **schema.get("$defs", {})}.values():
        props.update(group.get("properties", {}))
    if prop not in props:
        raise ValueError(f"Schema has no property '{prop}'")
    if "enum" not in props[prop]:
        raise ValueError(f"Schema property '{prop}' has no 'enum'")
    return props[prop]["enum"]


def test_steps_table_is_nonempty() -> None:
    steps = extract_step_names()
    assert steps, "ParamUtils.STEPS parsed to zero step names -- regex or table broke"


def test_entry_column_is_in_required_columns_for_every_step() -> None:
    """Each step's entryColumn must be one of its own requiredColumns.

    entryColumn is the checkpoint-CSV column INPUT_CHECK reads when that step is
    the run's --start point; requiredColumns is what CsvUtils.validateInputCSV
    demands be present for that same entry point. If entryColumn were not itself
    in requiredColumns, a samplesheet that validateInputCSV happily accepts as a
    valid entry point for the step could still be missing the very column
    INPUT_CHECK is about to read.

    No local nf-test starts at 'registration' or 'postprocessing' (every pipeline
    nf-test uses the default/'preprocessing' --start), so this relationship is
    otherwise exercised for exactly one of the three STEPS rows.
    """
    entries = extract_step_entries()
    assert entries, "no step entries parsed -- did STEPS get reshaped?"
    for entry in entries:
        assert entry["entryColumn"] in entry["requiredColumns"], (
            f"STEPS['{entry['name']}'].entryColumn ({entry['entryColumn']!r}) is not "
            f"one of its own requiredColumns ({entry['requiredColumns']}) -- a "
            "samplesheet CsvUtils.validateInputCSV accepts as this step's entry "
            "point could still be missing the column INPUT_CHECK reads."
        )


def test_schema_start_enum_matches_steps() -> None:
    steps = extract_step_names()
    schema = json.loads(SCHEMA_PATH.read_text())
    start_enum = extract_schema_enum(schema, "start")

    assert start_enum == steps, (
        "nextflow_schema.json's --start enum has drifted from ParamUtils.STEPS.\n"
        f"  schema:           {start_enum}\n"
        f"  ParamUtils.STEPS: {steps}\n"
        "Update nextflow_schema.json's 'start' enum to match -- STEPS is the "
        "source of truth and the schema is not generated from it."
    )


def test_schema_stop_enum_matches_steps_plus_null() -> None:
    steps = extract_step_names()
    schema = json.loads(SCHEMA_PATH.read_text())
    stop_enum = extract_schema_enum(schema, "stop")

    # --stop additionally accepts null (unset -> run to the last step).
    assert stop_enum == steps + [None], (
        "nextflow_schema.json's --stop enum has drifted from ParamUtils.STEPS.\n"
        f"  schema:                 {stop_enum}\n"
        f"  ParamUtils.STEPS + null: {steps + [None]}\n"
        "Update nextflow_schema.json's 'stop' enum to match -- STEPS is the "
        "source of truth and the schema is not generated from it."
    )


def test_no_parallel_entry_column_mapping_outside_param_utils() -> None:
    """No file other than lib/ParamUtils.groovy may quote 2+ of STEPS'
    entryColumn values -- that is exactly the textual shape of a parallel
    step->column mapping (e.g. the `pathColumn` map that used to live in
    lib/CsvUtils.groovy's `validateInputSemantics`). A single one of these
    literals elsewhere is fine and expected (e.g. workflows/mirage.nf hardcodes
    'path_to_file' once, for add_cycle's fixed preprocessing entry point) --
    it is *two or more together* that means an independent step vocabulary is
    being restated rather than read from ParamUtils.STEPS.
    """
    entry_columns = extract_entry_columns()
    assert len(entry_columns) >= 2, (
        "Fewer than 2 entryColumn values parsed from ParamUtils.STEPS -- "
        "this check needs at least 2 to detect a 'quoted together' pattern."
    )

    offenders: list[str] = []
    for pattern in SCANNED_GLOBS:
        for path in ROOT.glob(pattern):
            if path == PARAM_UTILS_PATH:
                continue
            text = path.read_text()
            found = [col for col in entry_columns if f"'{col}'" in text]
            if len(found) >= 2:
                offenders.append(f"  {path.relative_to(ROOT)}: {found}")

    assert not offenders, (
        "Found file(s) other than lib/ParamUtils.groovy quoting 2+ of STEPS' "
        f"entryColumn values ({entry_columns}) -- this looks like a parallel "
        "step->column mapping outside ParamUtils.STEPS:\n" + "\n".join(offenders) +
        "\nRoute it through ParamUtils.entryColumnForStep(step) instead."
    )
