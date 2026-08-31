#!/usr/bin/env python3
"""Guard that the shipped `reg_micro_reg` default is `1`, everywhere it lives.

`reg_micro_reg` has NINE homes (2026-08-30 spec, Task 5 of the
registration-backend-surface plan): `nextflow.config` (the single source of
truth for the shipped default -- see `test_no_duplicate_param_defaults.py`),
`nextflow_schema.json` (the schema's own copy of that default, which
`validateParameters()` uses), `docs/parameters.md`'s parameter table, three
`docs/figures/*.html` supplementary schematics, two `params/*.json` presets,
and `bin/register.py`. None of these are generated from another -- each is
a hand-maintained restatement -- so a numeric change to one does not
propagate to the rest; this test is the check that they were all moved
together.

`bin/register.py` alone turned out to carry FOUR of its own restatements,
not one: `valis_registration()`'s docstring prose (the home the plan's brief
counted), its Python function-signature default (`micro_reg: int = ...`),
the `--micro-reg` argparse `default=`, and the `[default]` marker inside
that argument's `--help` text. A first pass of this guard checked only the
docstring and missed the other three -- caught in review, not by this file
-- so all four are asserted below, independently, each anchored to its own
syntactic shape rather than to the docstring's.

`conf/test.config` pins `reg_micro_reg = 0` deliberately (Phase 0c: micro-
registration OOMed the JVM on a 15.6 GiB CI runner) and is NOT one of the
nine homes -- it must keep diverging from the shipped default, so it is
asserted to still read `0`, not `1`.

A second, independent check below closes the prose trap the plan's brief
missed on first pass: the claim "the default is the maximum legal value"
appears in FOUR places, not two, and one of them reverses the word order
(`MAX default` in `docs/figures/pipeline-schematic.html`, vs. `default MAX`
elsewhere) -- a grep for the literal brief phrase would not have found it.
Once `reg_micro_reg`'s default is `1` (not `2`, the max of `{0, 1, 2}`),
every one of those four sentences is false, so this test asserts none of
them survive, in either word order, case-insensitively.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests.nfmodel import block_extent as _block_extent
from tests.nfmodel import strip_comments as _strip_comments

ROOT = Path(__file__).resolve().parent.parent

# The nine homes that must all read the shipped default, `1`.
NEXTFLOW_CONFIG = ROOT / "nextflow.config"
SCHEMA = ROOT / "nextflow_schema.json"
PARAMETERS_MD = ROOT / "docs" / "parameters.md"
PIPELINE_SCHEMATIC = ROOT / "docs" / "figures" / "pipeline-schematic.html"
QC_SCHEMATIC = ROOT / "docs" / "figures" / "qc-schematic.html"
REGISTRATION_SCHEMATIC = ROOT / "docs" / "figures" / "registration-schematic.html"
FULL_PIPELINE_PARAMS = ROOT / "params" / "full_pipeline.json"
REGISTRATION_ONLY_PARAMS = ROOT / "params" / "registration_only.json"
REGISTER_PY = ROOT / "bin" / "register.py"

# Deliberately NOT one of the nine homes -- see module docstring.
TEST_CONFIG = ROOT / "conf" / "test.config"

# The four prose homes making the now-false "default is the max" claim, and
# both spellings it can take.
PROSE_HOMES = [NEXTFLOW_CONFIG, SCHEMA, PARAMETERS_MD, PIPELINE_SCHEMATIC]
MAX_DEFAULT_PATTERN = re.compile(r"default\s+max|max\s+default", re.IGNORECASE)


def _read(path: Path) -> str:
    text = path.read_text()
    assert text, f"{path} is empty -- guard would pass vacuously"
    return text


def _parse_top_level_params_block(config_text: str) -> dict[str, str]:
    """Return {name: declared_value_text} for a top-level `params { ... }`
    block in a .config file.

    Mirrors `test_no_duplicate_param_defaults.py`'s `parse_declared_params`:
    the block's extent comes from `tests.nfmodel.block_extent`'s comment/
    string-aware brace walk (not a naive brace count, which a `{`/`}` inside
    a comment or quoted default could mis-balance), and comments are
    stripped via `tests.nfmodel.strip_comments` before the line-by-line
    `name = value` parse, so both `nextflow.config` and `conf/test.config`
    -- which share this exact shape -- go through the same model rather than
    a second private regex.
    """
    start = config_text.find("params {")
    assert start != -1, "Could not locate `params { ... }` block"
    brace_start = config_text.find("{", start)
    end = _block_extent(config_text, brace_start + 1)
    block = config_text[brace_start + 1 : end - 1]
    clean_block = _strip_comments(block)
    declared: dict[str, str] = {}
    for raw_line in clean_block.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$", line)
        if m:
            declared[m.group(1)] = m.group(2)
    return declared


def test_nextflow_config_default_is_one():
    declared = _parse_top_level_params_block(_read(NEXTFLOW_CONFIG))
    assert "reg_micro_reg" in declared, "reg_micro_reg not declared in nextflow.config's params {}"
    assert declared["reg_micro_reg"] == "1", (
        f"nextflow.config's reg_micro_reg default is {declared['reg_micro_reg']!r}, expected '1'"
    )


def test_schema_default_is_one():
    schema = json.loads(_read(SCHEMA))

    def _find(node):
        if isinstance(node, dict):
            if "reg_micro_reg" in node and isinstance(node["reg_micro_reg"], dict):
                return node["reg_micro_reg"]
            for value in node.values():
                found = _find(value)
                if found is not None:
                    return found
        return None

    prop = _find(schema)
    assert prop is not None, "reg_micro_reg property not found in nextflow_schema.json"
    assert prop.get("default") == 1, (
        f"nextflow_schema.json's reg_micro_reg default is {prop.get('default')!r}, expected 1"
    )


def test_parameters_md_table_default_is_one():
    text = _read(PARAMETERS_MD)
    match = re.search(r"^\|\s*`reg_micro_reg`\s*\|\s*`(\S+)`\s*\|", text, re.MULTILINE)
    assert match, "reg_micro_reg row not found in docs/parameters.md"
    assert match.group(1) == "1", (
        f"docs/parameters.md's reg_micro_reg table default is {match.group(1)!r}, expected '1'"
    )


def test_pipeline_schematic_value_is_one():
    text = _read(PIPELINE_SCHEMATIC)
    match = re.search(
        r'<td class="k">reg_micro_reg</td><td class="v">(\S+?)</td>', text
    )
    assert match, "reg_micro_reg row not found in docs/figures/pipeline-schematic.html"
    assert match.group(1) == "1", (
        f"pipeline-schematic.html's reg_micro_reg value is {match.group(1)!r}, expected '1'"
    )


def test_qc_schematic_value_is_one():
    text = _read(QC_SCHEMATIC)
    match = re.search(
        r'<td class="k">reg_micro_reg</td><td class="v">(\S+?)</td>', text
    )
    assert match, "reg_micro_reg row not found in docs/figures/qc-schematic.html"
    assert match.group(1) == "1", (
        f"qc-schematic.html's reg_micro_reg value is {match.group(1)!r}, expected '1'"
    )


def test_registration_schematic_value_is_one():
    text = _read(REGISTRATION_SCHEMATIC)
    match = re.search(
        r'<td class="k">reg_micro_reg</td><td class="val">(\S+?)</td>', text
    )
    assert match, "reg_micro_reg row not found in docs/figures/registration-schematic.html"
    assert match.group(1) == "1", (
        f"registration-schematic.html's reg_micro_reg value is {match.group(1)!r}, expected '1'"
    )


def test_full_pipeline_params_default_is_one():
    params = json.loads(_read(FULL_PIPELINE_PARAMS))
    assert "reg_micro_reg" in params, "reg_micro_reg not present in params/full_pipeline.json"
    assert params["reg_micro_reg"] == 1, (
        f"params/full_pipeline.json's reg_micro_reg is {params['reg_micro_reg']!r}, expected 1"
    )


def test_registration_only_params_default_is_one():
    params = json.loads(_read(REGISTRATION_ONLY_PARAMS))
    assert "reg_micro_reg" in params, (
        "reg_micro_reg not present in params/registration_only.json"
    )
    assert params["reg_micro_reg"] == 1, (
        f"params/registration_only.json's reg_micro_reg is {params['reg_micro_reg']!r}, expected 1"
    )


def test_register_py_docstring_does_not_call_two_the_default():
    text = _read(REGISTER_PY)
    match = re.search(
        r"micro_reg\s*:\s*int, optional\n(?:.*\n)*?    (?:stage_checkpoint_dir|Returns)",
        text,
    )
    assert match, "micro_reg docstring block not found in bin/register.py"
    # Collapse whitespace first: docstring prose re-wraps across physical
    # lines whenever the surrounding text is edited, so a check anchored to
    # exact line breaks is one word-wrap away from a false negative.
    normalized = re.sub(r"\s+", " ", match.group(0))
    assert "matches the pipeline's ``reg_micro_reg`` default" in normalized, (
        "bin/register.py's cross-reference to the pipeline default is missing entirely "
        "-- expected wording to be reworded onto 1, not deleted"
    )
    # The sentence must now be anchored on "1 = micro-rigid only", not on
    # "2 = also the micro non-rigid pass" -- the old prose put "the default"
    # right after describing value 2.
    two_clause_match = re.search(r"2\s*=.*?\bthe default\b", normalized, re.IGNORECASE)
    assert two_clause_match is None, (
        "bin/register.py still describes micro_reg=2 as 'the default': "
        f"{two_clause_match.group(0) if two_clause_match else None!r}"
    )


def test_register_py_function_signature_default_is_one():
    """`valis_registration()`'s own Python default -- a direct import (not
    the pipeline, which always passes --micro-reg explicitly: register.nf)
    silently gets whatever this says."""
    text = _read(REGISTER_PY)
    match = re.search(r"^\s*micro_reg:\s*int\s*=\s*(\S+?),", text, re.MULTILINE)
    assert match, "micro_reg: int = ... signature default not found in bin/register.py"
    assert match.group(1) == "1", (
        f"bin/register.py's valis_registration() signature default is "
        f"{match.group(1)!r}, expected '1'"
    )


def test_register_py_argparse_default_is_one():
    """The `--micro-reg` CLI flag's own default -- a hand invocation without
    the flag silently gets whatever this says, independent of the pipeline
    (which always passes --micro-reg explicitly: register.nf)."""
    text = _read(REGISTER_PY)
    match = re.search(
        r'"--micro-reg",\s*\n\s*type=int,\s*\n\s*default=(\S+?),', text
    )
    assert match, "--micro-reg argparse block not found in bin/register.py"
    assert match.group(1) == "1", (
        f"bin/register.py's --micro-reg argparse default is {match.group(1)!r}, expected '1'"
    )


def test_register_py_help_text_marks_one_not_two_as_default():
    """The --micro-reg --help string's own '[default]' marker -- must sit on
    the '1=...' clause, not '2=...', or --help visibly lies to an operator
    about which value is shipped."""
    text = _read(REGISTER_PY)
    match = re.search(
        r'help="Micro-registration depth \(nested\):.*?"\s*\n\s*"[^"]*",',
        text,
        re.DOTALL,
    )
    assert match, "--micro-reg help text not found in bin/register.py"
    normalized = re.sub(r"\s+", " ", match.group(0))
    one_clause_match = re.search(r"1\s*=.*?\[default\]", normalized)
    assert one_clause_match, (
        f"bin/register.py's --micro-reg help text does not mark the '1=...' "
        f"clause as [default]: {normalized!r}"
    )
    two_clause_match = re.search(r"2\s*=.*?\[default\]", normalized)
    assert two_clause_match is None, (
        f"bin/register.py's --micro-reg help text still marks the '2=...' "
        f"clause as [default]: {normalized!r}"
    )


def test_conf_test_config_pin_is_untouched():
    """conf/test.config is a deliberate divergence from the shipped default,
    not one of the nine homes -- Phase 0c pinned it to 0 because micro-
    registration OOMed the JVM on a 15.6 GiB CI runner. It must stay 0."""
    declared = _parse_top_level_params_block(_read(TEST_CONFIG))
    assert "reg_micro_reg" in declared, "reg_micro_reg not declared in conf/test.config's params {}"
    assert declared["reg_micro_reg"] == "0", (
        f"conf/test.config's reg_micro_reg pin is {declared['reg_micro_reg']!r}, expected '0' "
        "(this file is deliberately NOT one of the nine homes -- see module docstring)"
    )


def test_no_prose_home_still_claims_the_default_is_the_maximum():
    """'default MAX' / 'MAX default' becomes false once the default is 1,
    the minimum non-zero value, not the maximum of {0, 1, 2}. Checked in
    both word orders, case-insensitively, across all four prose homes."""
    offenders = []
    for path in PROSE_HOMES:
        text = _read(path)
        if MAX_DEFAULT_PATTERN.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, (
        f"these files still claim the default is the maximum value: {offenders}"
    )
