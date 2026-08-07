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


def extract_step_names() -> list[str]:
    """The ordered `name` values inside ParamUtils.STEPS, parsed from source."""
    text = PARAM_UTILS_PATH.read_text()
    m = STEPS_BLOCK_RE.search(text)
    if m is None:
        raise ValueError(
            "Could not locate `static final List STEPS = [ ... ]` in "
            f"{PARAM_UTILS_PATH}. Did the table get renamed or reshaped?"
        )
    return STEP_NAME_RE.findall(m.group(1))


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
