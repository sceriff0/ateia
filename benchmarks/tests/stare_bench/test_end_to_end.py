import csv

import numpy as np
import pytest

from benchmarks.stare_bench.cli import _tre_summary, main, score_pair
from benchmarks.stare_bench.emit import ACCURACY_COLUMNS
from benchmarks.stare_bench.fields import make_field
from benchmarks.stare_bench.generate import generate_pair
from benchmarks.stare_bench.texture import SyntheticCropSource

pytestmark = pytest.mark.slow


def test_score_pair_produces_a_complete_row(tmp_path):
    pair = tmp_path / "pair"
    generate_pair(pair, (1024, 1024), seed=31, tile=256,
                  crop_source=SyntheticCropSource(),
                  field_params={"correlation_px": 333.0, "amplitude_px": 6.0},
                  physics_params={"photobleach": {"factor": 0.9},
                                  "blank_regions": {"fraction": 0.3}})
    row = score_pair(pair, tmp_path / "work", method="tiled", run_id="unit",
                     tile=256, halo=64, upsample=10, max_error=0.99)
    assert set(row) == set(ACCURACY_COLUMNS)
    assert row["epe_mean_px"] is not None
    assert row["gate_precision"] is not None


def test_cli_writes_the_table(tmp_path):
    pair = tmp_path / "pair"
    generate_pair(pair, (1024, 1024), seed=32, tile=256,
                  crop_source=SyntheticCropSource(),
                  physics_params={})
    out = tmp_path / "registration_synthetic_gt.csv"
    rc = main(["--pair-dir", str(pair), "--work-dir", str(tmp_path / "w"),
               "--run-id", "unit", "--tile", "256", "--halo", "64",
               "--out", str(out)])
    assert rc == 0
    with out.open() as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    assert rows[0]["method"] == "tiled"


def test_the_same_seed_scores_identically(tmp_path):
    rows = []
    for name in ("a", "b"):
        pair = tmp_path / f"pair_{name}"
        generate_pair(pair, (1024, 1024), seed=33, tile=256,
                      crop_source=SyntheticCropSource(), physics_params={})
        rows.append(score_pair(pair, tmp_path / f"w_{name}", method="tiled",
                               run_id="unit", tile=256, halo=64, upsample=10,
                               max_error=0.99))
    assert rows[0]["epe_mean_px"] == rows[1]["epe_mean_px"]
    assert rows[0]["param_hash"] == rows[1]["param_hash"]


def test_ground_truth_predictor_scores_near_zero_landmark_tre(tmp_path):
    """A predictor built from the GROUND-TRUTH field itself must recover its
    own landmarks almost exactly.

    This is the test that catches the sign/evaluation-point bug the brief's
    draft ``_tre_summary`` had (``moving - predict(target)``): a perfect
    predictor scores near-zero here only when ``_tre_summary`` evaluates the
    predictor AT the moving point and ADDS its output, matching
    ``truth["landmarks"]``'s convention of
    ``target = moving + field.sample(moving)``. Feeding the wrong line the
    exact field it should recover would still show a large residual -- on the
    order of the field amplitude, not a fraction of a pixel -- because it
    samples the field at the wrong point and subtracts instead of adding.
    """
    pair = tmp_path / "pair"
    truth = generate_pair(pair, (1024, 1024), seed=41, tile=256,
                          crop_source=SyntheticCropSource(),
                          field_params={"correlation_px": 333.0,
                                        "amplitude_px": 6.0},
                          physics_params={})
    field = make_field(truth["field_family"], tuple(truth["size"]),
                       **truth["field_params_call"])

    def perfect_predict(xy):
        return field.sample(xy)

    summary = _tre_summary(truth, perfect_predict)
    assert summary["mean_px"] < 1.0
    assert summary["median_px"] < 1.0


def _tre_summary_wrong(truth, predict):
    """The brief's original (incorrect) line, kept only to demonstrate the
    failure this task exists to fix. Not used by score_pair.
    """
    from benchmarks.registration_eval.landmarks import (
        image_diagonal,
        per_landmark_tre,
        summarize,
    )

    lm = np.asarray(truth["landmarks"], dtype=float)
    target, moving = lm[:, 0:2], lm[:, 2:4]
    warped = moving - predict(target)
    h, w = truth["size"]
    return summarize(per_landmark_tre(warped, target), image_diagonal(w, h))


def test_the_brief_s_original_line_fails_the_ground_truth_check(tmp_path):
    """Documents WHY the fix was needed: run the same ground-truth-predictor
    check against the brief's wrong ``moving - predict(target)`` line and
    show it does NOT come out near zero.
    """
    pair = tmp_path / "pair"
    truth = generate_pair(pair, (1024, 1024), seed=41, tile=256,
                          crop_source=SyntheticCropSource(),
                          field_params={"correlation_px": 333.0,
                                        "amplitude_px": 6.0},
                          physics_params={})
    field = make_field(truth["field_family"], tuple(truth["size"]),
                       **truth["field_params_call"])

    def perfect_predict(xy):
        return field.sample(xy)

    summary = _tre_summary_wrong(truth, perfect_predict)
    # Well above a pixel -- on the order of the field's own amplitude -- which
    # is exactly the garbage-but-plausible-looking column this task exists to
    # prevent from shipping.
    assert summary["mean_px"] > 1.0
