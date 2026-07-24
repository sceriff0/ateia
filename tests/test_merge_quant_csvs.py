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
