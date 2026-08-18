import json

import pandas as pd

from benchmarks.registration_eval import aggregate_eval


def _write(p, rec):
    p.write_text(json.dumps(rec))


def test_aggregate_flattens_records_and_computes_anhir_aggregates(tmp_path):
    _write(tmp_path / "e1.json", {
        "pair_id": "p1", "mode": "tiled",
        "true_tre": {"median_px": 4.0, "median_rtre": 0.04, "p90_px": 6.0},
        "valis_rtre": 0.05,
    })
    _write(tmp_path / "e2.json", {
        "pair_id": "p2", "mode": "tiled",
        "true_tre": {"median_px": 6.0, "median_rtre": 0.06, "p90_px": 8.0},
        "valis_rtre": 0.07,
    })
    df, agg = aggregate_eval.aggregate(tmp_path)
    assert set(df.columns) >= {"pair_id", "mode", "true_median_px", "true_median_rtre",
                               "valis_rtre", "stare_final_p50_px", "seg_dice_matched"}
    assert len(df) == 2
    # MMrTRE = median of per-pair median rTRE; AMrTRE = mean of them
    row = agg[agg["mode"] == "tiled"].iloc[0]
    assert row["MMrTRE"] == 0.05   # median(0.04, 0.06)
    assert row["AMrTRE"] == 0.05   # mean(0.04, 0.06)


def test_aggregate_keeps_sparse_method_native_columns(tmp_path):
    """A valis row and a STARE row carry different native signals.

    Both column families must still exist on both rows (NaN where absent) — the
    analysis pivots on `mode`, so a column that appears only for one method would
    break the paired comparison rather than just being empty.
    """
    _write(tmp_path / "v.json", {
        "pair_id": "p1", "mode": "valis",
        "true_tre": {"median_px": 4.0, "median_rtre": 0.04},
        "valis_rtre": 0.05, "stare_tre": None,
        "seg_qc": {"stage": "non_rigid", "dice_matched": 0.9,
                   "median_displacement_um": 0.8, "n_pairs": 100},
    })
    _write(tmp_path / "s.json", {
        "pair_id": "p1", "mode": "tiled",
        "true_tre": {"median_px": 5.0, "median_rtre": 0.05},
        "valis_rtre": None,
        "stare_tre": {"coarse_tre_px": 2.0, "rigid_p50_px": 3.0,
                      "final_p50_px": 0.6, "mesh_refined": True},
        "seg_qc": None,
    })
    df, _ = aggregate_eval.aggregate(tmp_path)
    by_mode = df.set_index("mode")
    assert by_mode.loc["tiled", "stare_final_p50_px"] == 0.6
    assert by_mode.loc["valis", "seg_dice_matched"] == 0.9
    # the absent halves are present-but-null, not missing columns
    assert "stare_final_p50_px" in df.columns and "seg_dice_matched" in df.columns
    assert pd.isna(by_mode.loc["valis", "stare_final_p50_px"])
    assert pd.isna(by_mode.loc["tiled", "seg_dice_matched"])
