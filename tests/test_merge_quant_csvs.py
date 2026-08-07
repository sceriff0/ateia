import importlib.util
from pathlib import Path

import pandas as pd

# Import the bin script as a module (it inserts bin/utils on sys.path at import).
_SPEC = importlib.util.spec_from_file_location(
    "merge_quant_csvs",
    Path(__file__).resolve().parent.parent / "bin" / "merge_quant_csvs.py",
)
mqc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mqc)


def _write_csv(tmp_path, name, frame):
    p = tmp_path / name
    frame.to_csv(p, index=False)
    return p


def test_new_marker_appends_as_column(tmp_path):
    base = pd.DataFrame({"label": [1, 2, 3], "area": [10, 20, 30]})
    new = _write_csv(
        tmp_path,
        "c2_CD8_quant.csv",
        pd.DataFrame({"label": [1, 2, 3], "CD8": [5.0, 6.0, 7.0]}),
    )
    out = mqc.merge_intensities(base, [new])
    assert list(out["CD8"]) == [5.0, 6.0, 7.0]
    assert len(out) == 3  # no cells gained/lost


def test_colliding_marker_new_cycle_wins(tmp_path):
    # Base already has a PANCK column (from cycle 1); new cycle re-measures PANCK.
    base = pd.DataFrame({"label": [1, 2], "area": [10, 20], "PANCK": [1.0, 2.0]})
    new = _write_csv(
        tmp_path,
        "c2_PANCK_quant.csv",
        pd.DataFrame({"label": [1, 2], "PANCK": [99.0, 98.0]}),
    )
    out = mqc.merge_intensities(base, [new])
    # New cycle overwrites; no pandas _x/_y suffix columns survive.
    assert "PANCK_x" not in out.columns and "PANCK_y" not in out.columns
    assert list(out["PANCK"]) == [99.0, 98.0]


def test_dapi_is_protected_from_overwrite(tmp_path):
    base = pd.DataFrame({"label": [1, 2], "DAPI": [100.0, 200.0]})
    new = _write_csv(
        tmp_path,
        "c2_DAPI_quant.csv",
        pd.DataFrame({"label": [1, 2], "DAPI": [1.0, 2.0]}),
    )
    out = mqc.merge_intensities(base, [new], protected_cols=("DAPI",))
    # Base DAPI kept, incoming ignored.
    assert list(out["DAPI"]) == [100.0, 200.0]


def test_reorder_columns_preserves_prior_cell_size_without_area():
    """Regression (add_cycle path): a prior merged table can carry fov/cell_size
    without an ``area`` column (e.g. an older schema, or cell_size computed some
    other way). Before adopting the canonical 12-entry MORPHOLOGY_COLS (which
    includes fov/cell_size), a hardcoded ``col not in ("fov", "cell_size")``
    clause excluded them from BOTH morpho_present and marker_cols_all, so the
    `merged[final_column_order]` column selection dropped them entirely.
    cell_size was only ever recreated from `area` a few lines later, so a prior
    cell_size with no matching area column was silently lost forever.

    With the canonical 12-entry list, fov/cell_size are ordinary morphology
    columns and survive the selection unconditionally.
    """
    prior = pd.DataFrame(
        {
            "label": [1, 2, 3],
            "fov": ["prior_patient"] * 3,
            "cell_size": [111.0, 222.0, 333.0],
            "DAPI": [1.0, 2.0, 3.0],
        }
    )
    out = mqc.reorder_columns(prior.copy(), patient_id="cycle2_patient")

    # cell_size survives from the base table exactly -- no `area` column
    # existed, so it is never overwritten/recomputed. This is the bug fix.
    assert "cell_size" in out.columns
    assert list(out["cell_size"]) == [111.0, 222.0, 333.0]

    # fov is still (unconditionally, unrelated to this change) refreshed to
    # the current patient_id -- required for Pixie fov tagging.
    assert list(out["fov"]) == ["cycle2_patient"] * 3
