import json

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
                               "valis_rtre"}
    assert len(df) == 2
    # MMrTRE = median of per-pair median rTRE; AMrTRE = mean of them
    row = agg[agg["mode"] == "tiled"].iloc[0]
    assert row["MMrTRE"] == 0.05   # median(0.04, 0.06)
    assert row["AMrTRE"] == 0.05   # mean(0.04, 0.06)
