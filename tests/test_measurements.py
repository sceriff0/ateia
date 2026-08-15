"""Unit tests for bin/utils/measurements.py: the single-owner measurement vocabulary.

Two things used to be declared independently (kept in sync only by a
comment) across bin/merge_quant_csvs.py, bin/export_geojson.py,
bin/export_spatialdata.py, bin/generate_postprocessing_qc.py, and
bin/quantify.py:

- the 12-entry morphology column list, in three different container types
  (list/set/tuple) and one under a different name (MORPHOLOGY_COLUMNS in
  generate_postprocessing_qc.py), with merge_quant_csvs.py's copy actually
  short by two entries (fov, cell_size);
- the measurement-key grammar "<marker>: <Compartment>: <Statistic>", built
  independently in quantify.py and phenotype_cells.py and parsed in
  export_spatialdata.py.

This test asserts all four former copies now resolve back to the same
canonical set, and pins measurement_key()'s exact output (G5: a
case-/space-sensitive contract with the sibling qupath-extension-flowpath
repo).
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / "bin"
sys.path.insert(0, str(BIN / "utils"))

import measurements as m  # noqa: E402


def _load_bin_module(name: str):
    """Load a bin/*.py script as a module (it inserts bin/utils on sys.path itself)."""
    spec = importlib.util.spec_from_file_location(name, BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── MORPHOLOGY_COLS: one owner ──────────────────────────────────────────────────
def test_canonical_morphology_cols_is_12_entry_tuple():
    assert isinstance(m.MORPHOLOGY_COLS, tuple)
    assert len(m.MORPHOLOGY_COLS) == 12
    assert set(m.MORPHOLOGY_COLS) == {
        "label",
        "y",
        "x",
        "area",
        "eccentricity",
        "perimeter",
        "convex_area",
        "axis_major_length",
        "axis_minor_length",
        "solidity",
        "fov",
        "cell_size",
    }


def test_all_former_copies_resolve_to_the_canonical_set():
    """The four former independent copies must now all equal the shared set.

    merge_quant_csvs.py (a list), export_geojson.py (a set),
    export_spatialdata.py (a tuple), and generate_postprocessing_qc.py's
    MORPHOLOGY_COLUMNS (a set) were each declared by hand; merge_quant_csvs.py's
    was short by `fov` and `cell_size`. All four must now be identical sets
    sourced from bin/utils/measurements.py.
    """
    canonical = set(m.MORPHOLOGY_COLS)

    mqc = _load_bin_module("merge_quant_csvs")
    eg = _load_bin_module("export_geojson")
    esd = _load_bin_module("export_spatialdata")
    qc = _load_bin_module("generate_postprocessing_qc")

    assert set(mqc.MORPHOLOGY_COLS) == canonical
    assert set(eg.MORPHOLOGY_COLS) == canonical
    assert set(esd.MORPHOLOGY_COLS) == canonical
    assert set(qc.MORPHOLOGY_COLUMNS) == canonical

    # Container semantics preserved at each call site.
    assert isinstance(mqc.MORPHOLOGY_COLS, list)
    assert isinstance(eg.MORPHOLOGY_COLS, set)
    assert isinstance(esd.MORPHOLOGY_COLS, tuple)
    assert isinstance(qc.MORPHOLOGY_COLUMNS, set)


def test_quantify_compartment_names_matches_canonical_compartments():
    quantify = _load_bin_module("quantify")
    assert quantify.COMPARTMENT_NAMES == m.COMPARTMENTS


# ── measurement_key(): the G5 contract ──────────────────────────────────────────
def test_measurement_key_exact_literal_string():
    """G5: exact spacing and case, pinned as a literal.

    This is the format qupath-extension-flowpath parses from GeoJSON
    measurement names ("marker: Compartment: Statistic"), reproduced from
    quantify.py::compute_compartment_intensities
    (`f"{channel_name}: {comp}: Median"`, bin/quantify.py:180) and
    phenotype_cells.py::_marker_values
    (`f"{marker}: {spec['compartment']}: {spec['statistic']}"`,
    bin/phenotype_cells.py:58, pre-refactor).
    """
    assert m.measurement_key("CD3", "Nucleus", "Median") == "CD3: Nucleus: Median"


def test_quantify_calls_the_shared_builder_rather_than_agreeing_with_it(monkeypatch):
    """SINGLE producer, not two implementations that happen to agree.

    This replaced `test_measurement_key_matches_quantify_producer_output`, which
    asserted that `measurement_key()` reproduced a key `quantify.py` built for
    itself with an f-string. Two implementations agreeing today is exactly what a
    contract with a sibling repo cannot rely on -- and that assertion was the
    admission the extraction never landed: `measurement_key` had ZERO production
    callers when it was written.

    Redefining the builder must move the producer's output. If quantify.py goes
    back to spelling the grammar itself, the substituted builder is ignored and
    the columns come out in the real format -- which is the failure below.
    """
    import numpy as np

    quantify = _load_bin_module("quantify")
    monkeypatch.setattr(
        quantify, "measurement_key", lambda mk, comp, stat: f"<{mk}|{comp}|{stat}>"
    )

    cell_mask = np.array([[1, 1], [2, 2]], dtype=np.int32)
    channel = np.array([[10.0, 20.0], [30.0, 40.0]])
    df = quantify.compute_compartment_intensities(
        cell_mask, None, channel, "CD3", statistics=["Median"]
    )
    assert list(df.columns) == ["label", "CD3", "<CD3|Cell|Median>"]


def test_export_spatialdata_parser_derives_its_suffix_from_the_builder(monkeypatch):
    """The parser is the same grammar read backwards, so it gets it from one place.

    `parse_measurement_key` used to rebuild `f": {comp}: {stat}"` itself -- a
    fourth copy of the separators, on the CONSUMING side, where a divergence
    reads as "this marker has no compartment" rather than as an error.
    """
    esd = _load_bin_module("export_spatialdata")
    monkeypatch.setattr(
        esd, "measurement_key", lambda mk, comp, stat: f"{mk}<|{comp}|{stat}"
    )
    assert esd.parse_measurement_key("PanCK<|Cytoplasm|Sum") == (
        "PanCK",
        "Cytoplasm",
        "Sum",
    )


def test_measurement_key_matches_export_spatialdata_parser():
    esd = _load_bin_module("export_spatialdata")
    key = m.measurement_key("PanCK", "Cytoplasm", "Sum")
    assert esd.parse_measurement_key(key) == ("PanCK", "Cytoplasm", "Sum")


# ── identify_marker_columns(): the shared predicate ─────────────────────────────
def test_identify_marker_columns_excludes_morphology_and_non_numeric():
    df = pd.DataFrame(
        {
            "label": [1, 2],
            "x": [1.0, 2.0],
            "y": [3.0, 4.0],
            "fov": ["p1", "p1"],
            "cell_size": [10, 20],
            "CD3: Cell: Median": [1.5, 2.5],
            "DAPI": [7.0, 8.0],
            "some_text": ["a", "b"],
        }
    )
    assert m.identify_marker_columns(df) == ["CD3: Cell: Median", "DAPI"]


# ── the phenotype measurement namespace ───────────────────────────────────────


def test_pheno_key_uses_a_dot_separator_not_a_colon_space():
    """A ': ' separator lets the FlowPath side's collapseToBaseMarkers fall through to
    the raw name and register a PHANTOM marker channel."""
    from measurements import PHENO_PREFIX, pheno_key

    assert PHENO_PREFIX == "_pheno."
    assert pheno_key("free_mask") == "_pheno.free_mask"
    assert pheno_key("score", "T_helper") == "_pheno.score.T_helper"
    assert ": " not in pheno_key("score", "T_helper")


def test_pheno_keys_never_collide_with_the_quantification_grammar():
    from measurements import COMPARTMENTS, STATISTICS, measurement_key, pheno_key

    quant = {measurement_key("CD3", c, s) for c in COMPARTMENTS for s in STATISTICS}
    pheno = {pheno_key("score", "CD3"), pheno_key("free_mask"), pheno_key("density_bin")}
    assert quant.isdisjoint(pheno)


def test_pheno_prefix_has_exactly_one_owner():
    """No second copy of the literal prefix anywhere in bin/."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "bin"
    offenders = [
        str(p.relative_to(root))
        for p in root.rglob("*.py")
        if p.name != "measurements.py" and '"_pheno.' in p.read_text()
    ]
    assert offenders == [], offenders


# ── measurement_key() is the SOLE producer, and its docstring says so truthfully ──
GRAMMAR_FSTRING_RE = re.compile(r'f"\{[^"}]*\}: \{[^"}]*\}: \{')
# A use of the shared builder, excluding `parse_measurement_key` (the `_` before it is
# a word character, so the lookbehind rejects it).
CALLS_BUILDER_RE = re.compile(r"(?<!\w)measurement_key\b")
# How the docstring names a caller: ``module.py::function``.
DOCSTRING_CALLER_RE = re.compile(r"``(\w+)\.py::")


# The regex is STRUCTURAL -- "three interpolations separated by ': '" -- so it also
# matches strings that merely share that shape. Exactly one such line exists, and it is
# allowlisted BY REASON, not by silence: stage_checkpoint.py builds a
# "<name>: <ExceptionType>: <message>" error line and has nothing to do with the
# measurement contract. `test_the_grammar_allowlist_entry_is_outside_the_contract`
# checks that reason instead of trusting this comment -- an allowlist entry whose
# stated reason nothing verifies is how these guards go quiet.
GRAMMAR_ALLOWLIST = {
    "bin/utils/stage_checkpoint.py": 'errors.append(f"{name}: {type(e).__name__}: {e}")',
}


def _bin_python_files():
    return sorted(
        p
        for p in BIN.rglob("*.py")
        if p.name != "measurements.py" and "__pycache__" not in p.parts
    )


def test_no_bin_module_spells_the_measurement_grammar_itself():
    """One producer, checked across all of bin/ rather than just quantify.py.

    `measurement_key()`'s docstring now says "do NOT reproduce the format
    anywhere else". This is what makes that sentence enforceable: a second
    speller anywhere under bin/ is a silent cross-repo break, because
    `parse_measurement_key` answers a mismatch with `(key, None, None)` -- "this
    marker has no compartment" -- rather than raising.
    """
    offenders = []
    for path in _bin_python_files():
        rel = str(path.relative_to(REPO_ROOT))
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if not GRAMMAR_FSTRING_RE.search(line):
                continue
            if GRAMMAR_ALLOWLIST.get(rel) == line.strip():
                continue
            offenders.append(f"{rel}:{i}: {line.strip()}")
    assert offenders == [], (
        "the '<marker>: <Compartment>: <Statistic>' grammar is spelled outside "
        "bin/utils/measurements.py:\n" + "\n".join(offenders)
    )


def test_measurement_key_docstring_names_exactly_its_real_callers():
    """The docstring's caller list is checked, not asserted.

    `measurement_key()` spent its whole first life documented as "reproduces the
    format independently built by quantify.py" while having ZERO production
    callers -- a comment that made the duplication read as the design. Its
    replacement names every caller, which is only an improvement if the list
    cannot rot. Set equality in BOTH directions: a new caller that nobody adds to
    the list fails here, and so does a listed caller that stopped calling.

    "Calls" is textual -- the module NAMES the symbol -- so importing it under an
    alias still counts. That is deliberate: the thing being pinned is which
    modules depend on the shared builder, not which local name they give it.

    Watched fail both ways: importing `measurement_key` into export_geojson.py
    reports `calling but not named: ['export_geojson']`; dropping it from
    export_spatialdata.py reports `named but not calling: ['export_spatialdata']`.
    """
    doc = m.measurement_key.__doc__
    named = set(DOCSTRING_CALLER_RE.findall(doc))
    actual = {
        path.stem
        for path in _bin_python_files()
        if CALLS_BUILDER_RE.search(path.read_text())
    }
    assert named == actual, (
        "measurement_key()'s docstring lists its callers and the list is stale.\n"
        f"  named but not calling: {sorted(named - actual)}\n"
        f"  calling but not named: {sorted(actual - named)}"
    )


def test_the_grammar_allowlist_entry_is_outside_the_contract():
    """Every GRAMMAR_ALLOWLIST entry must still be a non-measurement file.

    The stated reason is "this module has nothing to do with the measurement
    contract", and the mechanical form of that is: it does not import from
    `measurements` at all. If it ever does, its ': '-separated f-string stops
    being obviously unrelated and the allowlist entry has to be re-argued.

    Watched fail: adding `from measurements import COMPARTMENTS` to
    bin/utils/stage_checkpoint.py fails here while every other test stays green.
    """
    for rel, line in GRAMMAR_ALLOWLIST.items():
        path = REPO_ROOT / rel
        src = path.read_text()
        assert line in src, (
            f"{rel} no longer contains the allowlisted line; drop the entry:\n  {line}"
        )
        assert "measurements" not in src, (
            f"{rel} now imports the measurement vocabulary, so its "
            "'<a>: <b>: <c>' f-string can no longer be assumed unrelated to the "
            "measurement key. Re-argue or remove the GRAMMAR_ALLOWLIST entry."
        )
