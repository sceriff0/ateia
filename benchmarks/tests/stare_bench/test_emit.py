import csv

import pytest

from benchmarks.stare_bench.emit import (
    ACCURACY_COLUMNS,
    accuracy_row,
    write_accuracy_csv,
)

TRUTH = {"param_hash": "abc123", "seed": 7, "generator_version": "0.1.0",
         "field_family": "random_fourier",
         "field_params": {"correlation_px": 333.0, "amplitude_px": 6.0}}
EPE = {"mean_px": 1.2, "median_px": 1.0, "p95_px": 3.0, "max_px": 9.0, "n": 100,
       "subsample_effective": 1, "n_percentile": 100}
GATE = {"precision": 0.9, "recall": 0.8, "f1": 0.85, "tp": 8, "fp": 1, "tn": 5,
        "fn": 2, "n": 16}
JAC = {"folding_rate": 0.0, "det_min": 0.9, "det_p05": 0.95, "det_median": 1.0,
       "n": 100}
LIP = {"lipschitz": 0.4, "converges": True, "n": 100}
TRE = {"n": 200, "median_px": 1.1, "mean_px": 1.3, "p90_px": 2.4,
       "median_rtre": 0.0004, "mean_rtre": 0.0005}


def _row(**kw):
    base = dict(run_id="r1", method="tiled", pair_id="p1", truth=TRUTH,
                epe_stats=EPE, gate_stats=GATE, gate_auc_value=0.93,
                jac_stats=JAC, lip_stats=LIP, tre_summary=TRE)
    base.update(kw)
    return accuracy_row(**base)


def test_row_carries_every_declared_column():
    row = _row()
    assert set(row) == set(ACCURACY_COLUMNS)


def test_provenance_travels_with_the_numbers():
    row = _row()
    assert row["param_hash"] == "abc123"
    assert row["seed"] == 7
    assert row["generator_version"] == "0.1.0"


def test_epe_subsample_effective_reports_whether_percentiles_were_exact():
    row = _row()
    assert row["epe_subsample_effective"] == 1
    estimated = _row(epe_stats={**EPE, "subsample_effective": 37})
    assert estimated["epe_subsample_effective"] == 37


def test_intrinsic_tre_is_a_diagnostic_not_an_accuracy_column():
    row = _row(intrinsic_tre=0.42)
    assert row["diag_intrinsic_tre_px"] == 0.42
    # It must not appear under any name a reader would mistake for accuracy.
    for name in ("epe_mean_px", "tre_median_px", "accuracy"):
        assert row.get(name) != 0.42


def test_a_self_reported_metric_cannot_be_smuggled_into_an_accuracy_column():
    with pytest.raises(ValueError, match="diagnostic"):
        accuracy_row(run_id="r1", method="tiled", pair_id="p1", truth=TRUTH,
                     epe_stats={**EPE, "intrinsic_tre": 0.4}, gate_stats=GATE,
                     gate_auc_value=0.9, jac_stats=JAC, lip_stats=LIP,
                     tre_summary=TRE)


def test_a_read_stare_tre_spelling_of_the_same_residual_is_also_forbidden():
    # benchmarks/stare_bench/tre.py:read_stare_tre re-flattens the SAME
    # circular STARE post-mesh residual under this name; it must be caught too.
    with pytest.raises(ValueError, match="diagnostic"):
        accuracy_row(run_id="r1", method="tiled", pair_id="p1", truth=TRUTH,
                     epe_stats=EPE, gate_stats=GATE, gate_auc_value=0.9,
                     jac_stats=JAC, lip_stats=LIP,
                     tre_summary={**TRE, "final_p50_px": 0.3})


def test_accuracy_row_requires_all_epe_stats_keys():
    incomplete = {k: v for k, v in EPE.items() if k != "subsample_effective"}
    with pytest.raises(ValueError, match="subsample_effective"):
        accuracy_row(run_id="r1", method="tiled", pair_id="p1", truth=TRUTH,
                     epe_stats=incomplete, gate_stats=GATE, gate_auc_value=0.9,
                     jac_stats=JAC, lip_stats=LIP, tre_summary=TRE)


def test_write_accuracy_csv_round_trips(tmp_path):
    out = tmp_path / "registration_synthetic_gt.csv"
    write_accuracy_csv([_row()], out)
    with out.open() as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    assert rows[0]["method"] == "tiled"
    assert list(rows[0]) == ACCURACY_COLUMNS


def test_write_accuracy_csv_accepts_a_generator_not_just_a_list(tmp_path):
    out = tmp_path / "registration_synthetic_gt.csv"
    rows = (_row(pair_id=f"p{i}") for i in range(2))
    write_accuracy_csv(rows, out)
    with out.open() as fh:
        written = list(csv.DictReader(fh))
    assert len(written) == 2
    assert {r["pair_id"] for r in written} == {"p0", "p1"}


def test_write_accuracy_csv_rejects_a_row_with_an_extra_key(tmp_path):
    row = {**_row(), "unexpected_extra_column": 1.0}
    with pytest.raises(ValueError, match="unexpected_extra_column"):
        write_accuracy_csv([row], tmp_path / "out.csv")


def test_write_accuracy_csv_rejects_a_row_missing_a_key(tmp_path):
    row = _row()
    del row["method"]
    with pytest.raises(ValueError, match="method"):
        write_accuracy_csv([row], tmp_path / "out.csv")
