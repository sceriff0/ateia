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
STATISTICS: tuple = ("Median", "Mean", "Sum")


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
