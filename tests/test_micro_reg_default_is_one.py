#!/usr/bin/env python3
"""Guard that the shipped `reg_micro_reg` default is `1`, everywhere it lives.

`reg_micro_reg` has TEN homes. NINE were enumerated by the 2026-08-30 spec
(Task 5 of the registration-backend-surface plan); the tenth --
`ParamUtils.microRegLevelOf`'s javadoc -- was found by the final whole-branch
review, and is documented at `PARAM_UTILS` below. The nine: `nextflow.config`
(the single source of
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
# The TENTH home, found by the final whole-branch review after this file was
# written: `ParamUtils.microRegLevelOf`'s javadoc said "Default 2 (max:
# micro-rigid + micro non-rigid), matching nextflow.config" -- a sentence THIS
# BRANCH made false. It escaped every check here twice over: it is not in the
# nine-home list, and `MAX_DEFAULT_PATTERN` could not reach it either, because
# the phrasing is "Default 2 (max: ...", which neither "default max" nor "max
# default" matches. A prose home needs a check shaped like its own prose.
PARAM_UTILS = ROOT / "lib" / "ParamUtils.groovy"
# ELEVENTH and TWELFTH, same review, same pattern: two published figures carry the
# value TWICE -- once in a `<td class="k">reg_micro_reg</td>` parameter row (which the
# checks above already pin) and once again in a `<span>micro_reg <b>N</b></span>`
# summary badge on the registration step, which nothing reached. Both badges still read
# `2`. A per-home check that matches only one of a file's two copies is not coverage of
# that file.
PIPELINE_MD = ROOT / "docs" / "pipeline.md"
# `docs/figures/registration-schematic.html` carries a THIRD such restatement
# ("register_micro() - micro_reg = 2 - default", :554). It is deliberately NOT asserted
# here: spec Phase 6 owns rewriting that figure wholesale, and half-correcting it would
# be worse than leaving it to its owner. Its parameter-table row IS pinned above.
_MICRO_REG_BADGE_RE = re.compile(r"<span>micro_reg <b>(\S+?)</b></span>")

# Deliberately NOT one of the nine homes -- see module docstring.
TEST_CONFIG = ROOT / "conf" / "test.config"

# The four prose homes making the now-false "default is the max" claim, and
# both spellings it can take.
PROSE_HOMES = [NEXTFLOW_CONFIG, SCHEMA, PARAMETERS_MD, PIPELINE_SCHEMATIC, PARAM_UTILS]
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


def test_pipeline_schematic_badge_is_one():
    """The step-summary badge, not the parameter row -- a SECOND copy in the
    same file, which `test_pipeline_schematic_value_is_one` above does not
    reach. It read `2` for the whole of this branch."""
    text = _read(PIPELINE_SCHEMATIC)
    match = _MICRO_REG_BADGE_RE.search(text)
    assert match, (
        "micro_reg summary badge not found in docs/figures/pipeline-schematic.html "
        "-- this check would pass vacuously"
    )
    assert match.group(1) == "1", (
        f"pipeline-schematic.html's micro_reg BADGE reads {match.group(1)!r}, expected "
        "'1' (its parameter-table row is checked separately -- both are homes)"
    )


def test_pipeline_md_badge_is_one():
    """docs/pipeline.md embeds the same step-summary badge."""
    text = _read(PIPELINE_MD)
    match = _MICRO_REG_BADGE_RE.search(text)
    assert match, (
        "micro_reg summary badge not found in docs/pipeline.md -- this check would "
        "pass vacuously"
    )
    assert match.group(1) == "1", (
        f"docs/pipeline.md's micro_reg badge reads {match.group(1)!r}, expected '1'"
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


def _doc_comment_above(text: str, signature_fragment: str) -> str:
    """The contiguous comment block immediately above the line containing
    `signature_fragment`, returned as normalised prose.

    Deliberately NOT a hand-rolled `/* ... */` regex. `tests/test_nfmodel.py::
    test_no_guard_parses_nextflow_source_privately` forbids one, and rightly:
    the measured case is `path(reference, stageAs: 'ref/*')` in
    `modules/local/register.nf`, whose `/*` opened a FAKE block comment that
    swallowed 64 lines from a naive DOTALL parse. The first draft of this
    helper WAS that regex and that guard caught it.

    So the location comes from the model instead. `tests.nfmodel.strip_comments`
    blanks comments to spaces while preserving line structure, so a line that
    is whitespace in the blanked view but non-empty in the raw file IS a comment
    line -- that difference is the extraction, and the model owns the hard part
    (knowing where a comment really starts and ends).
    """
    blanked = _strip_comments(text)
    raw_lines = text.splitlines()
    blank_lines = blanked.splitlines()
    assert len(raw_lines) == len(blank_lines), (
        "strip_comments changed the line count; the raw/blanked line pairing below "
        "would be meaningless"
    )
    idx = next(
        (i for i, line in enumerate(blank_lines) if signature_fragment in line), None
    )
    assert idx is not None, (
        f"{signature_fragment!r} not found in the code (comment-blanked) view -- "
        "either it was renamed, or it exists only inside a comment"
    )
    out: list[str] = []
    j = idx - 1
    while j >= 0 and raw_lines[j].strip() and not blank_lines[j].strip():
        out.append(raw_lines[j].strip().lstrip("*/ ").rstrip("*/ "))
        j -= 1
    assert out, f"no comment block sits immediately above {signature_fragment!r}"
    return re.sub(r"\s+", " ", " ".join(reversed(out)))


def test_param_utils_doc_comment_does_not_call_two_the_default():
    """`microRegLevelOf`'s javadoc is a tenth home, and this branch falsified it.

    It read "Default 2 (max: micro-rigid + micro non-rigid), matching
    nextflow.config" while nextflow.config had moved to 1. Two separate reasons
    nothing here caught it: the file was not in the nine-home list, and
    `MAX_DEFAULT_PATTERN` ("default max" / "max default") does not match
    "Default 2 (max: ...".

    The `regMicroReg == null ? 2 : ...` fallback VALUE is deliberately left
    alone -- it is unreachable on the pipeline path, since the schema declares
    `reg_micro_reg` non-nullable with `default: 1` and `validateParameters()`
    fills it in first -- so this asserts on the PROSE only, and requires the
    prose to say which of the two numbers ships.
    """
    # Prose re-wraps whenever the surrounding text is edited, so the helper
    # normalises whitespace before matching, as the bin/register.py checks above do.
    doc = _doc_comment_above(_read(PARAM_UTILS), "static int microRegLevelOf(")

    offender = re.search(r"[Dd]efault\s+(?:is\s+|of\s+)?2\b", doc)
    assert offender is None, (
        "lib/ParamUtils.groovy's microRegLevelOf javadoc still calls 2 the "
        f"default: {offender.group(0)!r} -- nextflow.config ships 1"
    )
    assert re.search(r"[Dd]efault\s+(?:is\s+)?1\b", doc), (
        "lib/ParamUtils.groovy's microRegLevelOf javadoc no longer names the "
        "shipped default at all -- expected it reworded onto 1, not deleted"
    )
    assert "nextflow.config" in doc, (
        "microRegLevelOf's javadoc dropped its cross-reference to the single "
        "source of truth; the whole point of naming the default here is to point "
        "a reader at where it is declared"
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
