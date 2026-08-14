"""Canonical measurement vocabulary shared across the postprocessing scripts.

Single source of truth for two things that used to be declared independently
in several `bin/*.py` scripts, kept in sync only by a comment:

- ``MORPHOLOGY_COLS``: the columns in the merged quantification CSV that
  describe cell geometry/identity rather than marker signal.
- The measurement-key grammar ``"<marker>: <Compartment>: <Statistic>"``
  produced by ``quantify.py`` and consumed by ``export_geojson.py``,
  ``export_spatialdata.py``, and ``phenotype_cells.py``.

G5 contract: the measurement-key format is consumed by the sibling repo
``qupath-extension-flowpath`` and is case- and space-sensitive. Do not change
``measurement_key()``'s output format, or the ``COMPARTMENTS``/``STATISTICS``
vocabularies, without a coordinated change on that side.

Import convention: this module is imported flat (``from measurements import
...``) by scripts that do ``sys.path.insert(0, .../bin/utils)`` first,
matching the existing convention in e.g. ``bin/export_geojson.py``. It is
pure-stdlib plus ``pandas``, which every consumer already depends on.
"""

from __future__ import annotations

from typing import List

import pandas as pd

# Canonical morphology/metadata columns (geometry + identity), in producer
# order. Every column here is NOT a marker intensity. This is the 12-entry
# list; former copies of this list disagreed on count (10 vs 12) and
# container type (list/set/tuple) -- callers wrap this tuple in the
# container their own local semantics need (e.g. `set(MORPHOLOGY_COLS)` for
# membership testing in a loop).
MORPHOLOGY_COLS: tuple = (
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
)

# The measurement-key grammar shared with QuPath/FlowPath:
# "<marker>: <Compartment>: <Statistic>".
COMPARTMENTS: tuple = ("Nucleus", "Cytoplasm", "Cell")

# ── the statistic vocabulary ──────────────────────────────────────────────────
# `params.quantify_statistics` selects a SUBSET of STATISTICS below. The list is
# COMPOSED, not hand-written: every base statistic crossed with every
# normalisation. Writing the twelve names out by hand is how two of them end up
# spelled differently.

# What is measured.
#
# REDSEA is the odd one out twice over: it is WHOLE-CELL ONLY (a membrane
# correction has no nucleus/cytoplasm decomposition, so `<marker>: Cell: REDSEA`
# exists while `<marker>: Nucleus: REDSEA` deliberately does not), and it is
# already size-normalised -- the reference implementation's recommended output,
# `dataRedSeaScaleSizeFCS.fcs`.
REDSEA_STATISTIC: str = "REDSEA"
COMPARTMENT_STATISTICS: tuple = ("Median", "Mean", "Sum")
BASE_STATISTICS: tuple = COMPARTMENT_STATISTICS + (REDSEA_STATISTIC,)

# How it is standardised across the cells of ONE PATIENT.
#
# Population: every cell of that patient, for that marker and compartment. That
# is exactly what a QUANTIFY task already holds, which is why per-patient z needs
# no aggregation stage -- a COHORT z, pooled across patients, would.
#
# Two flavours because they answer differently on multiplexed-IF data. Classic z
# uses mean/SD; a handful of very bright cells inflate SD and squash everyone
# else toward zero. Robust z uses median/MAD scaled by 1.4826 (which makes it
# consistent with SD for normally distributed data), and is the usual choice for
# skewed intensity distributions.
NORMALIZATIONS: tuple = ("", " Z", " RobustZ")

# Every name a `--quantify_statistics` list (or a panel spec) may carry, grouped
# by base statistic so a marker's variants stay adjacent in the output.
#
# G5 contract with qupath-extension-flowpath: these appear verbatim in the
# measurement key. Note that no name is a suffix of another once the compartment
# prefix is included -- "X: Cell: Median Z" does not end with ": Cell: Median" --
# so suffix-matching parsers stay unambiguous.
STATISTICS: tuple = tuple(
    base + norm for base in BASE_STATISTICS for norm in NORMALIZATIONS
)

# The default when `params.quantify_statistics` is unset. Declared here for
# standalone invocation of bin/quantify.py; nextflow.config carries the
# pipeline-facing default and is the only place a PARAMETER default may live.
DEFAULT_STATISTICS: tuple = ("Median",)


def split_statistic(statistic: str):
    """``"Mean RobustZ"`` -> ``("Mean", " RobustZ")``.

    The one place the composed name is taken apart, so producers and consumers
    cannot disagree about where the base ends and the normalisation begins.
    Longest normalisation first: " Z" is a suffix of nothing here, but " RobustZ"
    would be mis-split by a shorter match if the order were reversed.
    """
    for norm in sorted(NORMALIZATIONS, key=len, reverse=True):
        if norm and statistic.endswith(norm):
            return statistic[: -len(norm)], norm
    return statistic, ""


def zscore(values, robust: bool = False):
    """Standardise ``values`` across the cells of one patient.

    ``robust=False``  (x - mean) / SD
    ``robust=True``   (x - median) / (1.4826 * MAD)

    A ZERO SPREAD RETURNS ZEROS, not NaN or inf. Every cell carrying the same
    value really is exactly at the centre, so 0 is the correct z -- and a marker
    that is constant across a patient (an empty channel, or a compartment that
    never overlaps) is common enough that propagating inf would poison a gate
    rather than reporting one. Same reasoning as the neighbourless-cell guard in
    ``bin/utils/redsea.py``.
    """
    import numpy as np

    v = np.asarray(values, dtype=np.float64)
    if v.size == 0:
        return v
    if robust:
        centre = np.median(v)
        spread = 1.4826 * np.median(np.abs(v - centre))
    else:
        centre = v.mean()
        spread = v.std()
    if not np.isfinite(spread) or spread <= 0:
        return np.zeros_like(v)
    return (v - centre) / spread


def measurement_key(marker: str, compartment: str, statistic: str) -> str:
    """Build a measurement key: ``"<marker>: <Compartment>: <Statistic>"``.

    G5 contract with qupath-extension-flowpath: exact spacing and case.
    Reproduces the format independently built by
    ``quantify.py::compute_compartment_intensities`` (the producer) and
    ``phenotype_cells.py::_marker_values`` (a consumer), and parsed by
    ``export_spatialdata.py::parse_measurement_key``.
    """
    return f"{marker}: {compartment}: {statistic}"


def identify_marker_columns(df: "pd.DataFrame") -> List[str]:
    """Numeric columns that are marker measurements, not morphology/metadata.

    Shared predicate: "not in MORPHOLOGY_COLS and numeric dtype", in
    ``df.columns`` order. Formerly duplicated verbatim in
    ``export_geojson.py`` and ``export_spatialdata.py``.
    """
    return [
        col
        for col in df.columns
        if col not in MORPHOLOGY_COLS and pd.api.types.is_numeric_dtype(df[col])
    ]
