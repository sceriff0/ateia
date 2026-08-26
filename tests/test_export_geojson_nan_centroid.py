"""A cell with a NaN centroid must be skipped, not crash the combined export.

cells.geojson is the file qupath-extension-flowpath consumes. The regression:
export_combined_geojson() declared `skipped = 0` (a plain int, correct for this
function -- unlike the sibling export_geojson(), it has no closure that needs to
mutate the counter after the fact) but then incremented it as `skipped[0] += 1`,
copy-pasted from that sibling. The first NaN centroid raised
`TypeError: 'int' object is not subscriptable` and took the whole export down
with it, silently dropping every cell -- not just the bad one.
"""

from __future__ import annotations

import json
import logging

import numpy as np
import pandas as pd

# Imported at module scope (collection time), not inside the test body: this
# module's first import triggers bin/utils/logger.py's configure_logging(),
# which clears the root logger's handlers exactly once, process-wide. Pytest
# attaches its own per-test caplog handler during each test's *setup* phase,
# which runs after collection -- so importing here (during collection) keeps
# configure_logging()'s one-time handler-clear from ever racing caplog's
# handler, regardless of what other test files run before or after this one.
from export_geojson import export_combined_geojson


def test_nan_centroid_is_skipped_and_valid_cells_still_export(tmp_path, caplog):
    """One NaN-x row, one NaN-y row, one valid row: both bad rows drop, the good one survives."""
    df = pd.DataFrame(
        {
            "label": [1, 2, 3],
            "x": [10.0, np.nan, 30.0],
            "y": [10.0, 20.0, np.nan],
            "area": [100.0, 100.0, 100.0],
        }
    )
    cell_contours = {
        "1": [[5, 5], [15, 5], [15, 15], [5, 15], [5, 5]],
    }
    nucleus_contours = {}

    with caplog.at_level(logging.INFO, logger="export_geojson"):
        counts = export_combined_geojson(
            df,
            str(tmp_path),
            0.325,
            marker_cols=["area"],
            cell_contours=cell_contours,
            nucleus_contours=nucleus_contours,
            prefix="cells",
        )

    # The point is that this returns at all: before the fix, the first NaN
    # centroid (row 2, "label": 2) raised TypeError and the export never
    # completed.
    assert counts == {"cells": 1, "cells_wholecell": 1}

    combined = (tmp_path / "cells.geojson").read_text()
    gj = json.loads(combined)
    assert len(gj["features"]) == 1

    # The skipped count reported to the operator must reflect BOTH dropped
    # rows (NaN x and NaN y), not just the one that would previously have
    # crashed the process before ever reaching the second.
    skip_lines = [r.message for r in caplog.records if "skipped" in r.message]
    assert skip_lines, "expected a log line reporting the skipped-cell count"
    assert "skipped 2" in skip_lines[-1]
