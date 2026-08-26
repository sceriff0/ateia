"""Score one synthetic pair end to end and write the accuracy table."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from .emit import accuracy_row, write_accuracy_csv
from .fields import make_field
from .metrics.epe import epe
from .metrics.field_quality import jacobian_stats, lipschitz
from .metrics.gate_roc import gate_auc, gate_roc
from .run_unit import predict_from_manifest, run_stare

__all__ = ["score_pair", "main"]


def _tre_summary(truth, predict):
    """Landmark TRE through the SAME evaluator registration_eval already uses.

    Convention, derived (not guessed) from how the pair was generated:

    ``generate.py``'s ``_warp`` PULL-samples: ``mov(p) = ref(p + disp(p))`` for
    every moving-frame pixel ``p``. That makes the ground-truth field a
    function of MOVING-frame coordinates, and it is why ``truth["landmarks"]``
    is laid out as ``[target_x, target_y, moving_x, moving_y]`` with
    ``target = moving + field.sample(moving)`` -- the correspondence runs
    moving-to-target, sampled at the moving point.

    ``predict_from_manifest`` mirrors that: it returns
    ``warp(moving_name, xy, "refined") - xy``, i.e. the displacement from a
    MOVING-frame point ``xy`` to its predicted reference-frame location. So a
    perfect predictor satisfies ``moving + predict(moving) == target``, and
    scoring it means evaluating ``predict`` AT ``moving`` and ADDING -- not
    evaluating at ``target`` and subtracting, which reads backwards from both
    the frame ``predict`` expects its input in and the sign of the
    correspondence. (This is the second time this exact direction has been
    gotten wrong in this codebase; see ``generate.py:_landmarks`` for the
    matching correction on the ground-truth-generation side.)
    """
    from benchmarks.registration_eval.landmarks import (
        image_diagonal,
        per_landmark_tre,
        summarize,
    )

    lm = np.asarray(truth["landmarks"], dtype=float)
    if lm.size == 0:
        return {}
    target, moving = lm[:, 0:2], lm[:, 2:4]
    warped = moving + predict(moving)
    h, w = truth["size"]
    return summarize(per_landmark_tre(warped, target), image_diagonal(w, h))


def _reconstruct_accept(control, max_error, max_disp):
    """Mirror ``bin/tiled_solve.py:_accept`` exactly, including NaN handling.

    This is a RECONSTRUCTION from the control-point JSON, not a call into
    ``_accept`` itself, so it must match that function's rule precisely or the
    gate ROC measures a decision STARE never actually made. In particular:
    the range gate (``|d| >= max_disp``) is checked first and independently,
    a missing ``"error"`` key accepts unconditionally (legacy contract), and
    the confidence gate is written as ``not (error <= max_error)`` -- NOT
    ``error > max_error`` -- specifically so a NaN error (scikit-image's
    return for an empty crop, upstream issue #7078) REJECTS rather than
    silently passes a comparison against NaN.
    """
    if max_disp is not None:
        d = float(np.hypot(control["dx"], control["dy"]))
        if d >= max_disp:
            return False
    if "error" not in control:
        return True
    error = float(control["error"])
    if max_error is not None and not (error <= max_error):
        return False
    return True


def score_pair(pair_dir, work_dir, *, method, run_id, tile, halo, upsample,
               max_error):
    """Register one pair and reduce it to a single accuracy row."""
    pair_dir = Path(pair_dir)
    truth = json.loads((pair_dir / "truth.json").read_text())
    shape = tuple(truth["size"])
    field = make_field(truth["field_family"], shape, **truth["field_params_call"])

    result = run_stare(pair_dir, work_dir, tile=tile, halo=halo,
                       upsample=upsample, max_error=max_error)
    predict = predict_from_manifest(result["manifest"], "mov")

    accepted = {}
    scores = {}
    for c in result["controls"]:
        key = (int(c["ix"]), int(c["iy"]))
        scores[key] = float(c.get("error", float("nan")))
        accepted[key] = _reconstruct_accept(c, max_error, halo)

    intrinsic = None
    if result["tre_json"] is not None:
        doc = json.loads(Path(result["tre_json"]).read_text())
        block = doc.get("rigid_tre_px")
        intrinsic = block.get("p50") if isinstance(block, dict) else None

    return accuracy_row(
        run_id=run_id, method=method, pair_id=pair_dir.name, truth=truth,
        epe_stats=epe(field, predict, shape, tile=min(tile, 512)),
        gate_stats=gate_roc(truth["tile_labels"], accepted),
        gate_auc_value=gate_auc(truth["tile_labels"], scores),
        jac_stats=jacobian_stats(predict, shape, tile=min(tile, 512)),
        lip_stats=lipschitz(predict, shape, tile=min(tile, 512)),
        tre_summary=_tre_summary(truth, predict),
        intrinsic_tre=intrinsic,
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description="Score a synthetic STARE benchmark pair.")
    ap.add_argument("--pair-dir", required=True)
    ap.add_argument("--work-dir", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--method", default="tiled")
    ap.add_argument("--tile", type=int, default=2048)
    ap.add_argument("--halo", type=int, default=256)
    ap.add_argument("--upsample", type=int, default=10)
    ap.add_argument("--max-error", type=float, default=0.99)
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)

    row = score_pair(a.pair_dir, a.work_dir, method=a.method, run_id=a.run_id,
                     tile=a.tile, halo=a.halo, upsample=a.upsample,
                     max_error=a.max_error)
    write_accuracy_csv([row], a.out)
    print(f"Wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
