"""Score one synthetic pair end to end and write the accuracy table."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from .emit import accuracy_row, write_accuracy_csv
from .fields import make_field
from .metrics.epe import epe
from .metrics.field_quality import jacobian_stats, lipschitz
from .metrics.gate_roc import gate_auc, gate_roc
from .run_unit import BIN, predict_from_manifest, run_stare

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


def _accept_impl():
    """Import ``bin/tiled_solve.py``'s real ``_accept``, lazily.

    The gate ROC's validity rests on reconstructing exactly what the pipeline
    does, so this calls the pipeline's own rule rather than a local copy of
    it -- a copy could silently drift from ``_accept`` and the benchmark
    would then measure a decision STARE no longer makes. Mirrors
    ``run_unit.py:predict_from_manifest``'s own pattern for reaching into
    ``bin/`` at call time rather than at import time.
    """
    bin_dir = str(BIN)
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    from tiled_solve import _accept

    return _accept


def _reconstruct_accept(control, max_error, max_disp):
    """Whether ``bin/tiled_solve.py``'s gate would keep this control point.

    A thin wrapper around the real ``_accept`` (imported via ``_accept_impl``)
    so ``score_pair`` doesn't need to know ``_accept`` returns
    ``(accepted, reason)``.
    """
    accept = _accept_impl()
    return accept(control, max_error, max_disp)[0]


def _valis_moving_slide_name(registrar):
    """The registrar's single non-reference slide key.

    VALIS names each slide after its source filename (``valtils.get_name``),
    not any fixed literal -- so, like the manifest schema
    ``run_unit.moving_slide_name`` reads, there is no constant "mov" to rely
    on. ``registrar.get_ref_slide()`` gives the reference slide object
    directly; the moving slide is whatever else is in ``slide_dict``.
    """
    ref_name = registrar.get_ref_slide().name
    candidates = [k for k in registrar.slide_dict if k != ref_name]
    if len(candidates) != 1:
        raise ValueError(
            "expected exactly one non-reference slide in VALIS registrar, "
            f"found {candidates!r} (ref={ref_name!r}, "
            f"slides={list(registrar.slide_dict)!r})"
        )
    return candidates[0]


def _predict_from_valis_pickle(transform_path):
    """A ``(N, 2) -> (N, 2)`` displacement predictor from a VALIS registrar.

    Mirrors ``run_unit.predict_from_manifest``'s contract exactly (input a
    MOVING-frame point, return a displacement, not a position) so the SAME
    ``_tre_summary``/``epe``/``jacobian_stats``/``lipschitz`` calls in
    ``score_pair`` work unchanged regardless of which method produced the
    transform. ``registrar_slide.warp_xy`` returns a POSITION in the
    reference frame, so the displacement is that position minus the input --
    exactly what ``eval_tre.py``'s own landmark scoring does with the same
    call. Import is lazy: VALIS must not be needed to score a STARE run.
    """
    from benchmarks.registration_eval.eval_tre import default_loader

    registrar = default_loader(transform_path)
    moving_name = _valis_moving_slide_name(registrar)
    slide = registrar.slide_dict[moving_name]

    def predict(xy):
        xy = np.asarray(xy, dtype=float)
        warped = np.asarray(slide.warp_xy(xy, non_rigid=True), dtype=float)
        return warped - xy

    return predict


def score_pair(pair_dir, work_dir, *, method, run_id, tile, halo, upsample,
               max_error, transform_path=None):
    """Register one pair and reduce it to a single accuracy row.

    ``transform_path`` is the ALREADY-PRODUCED transform from a real pipeline
    run -- a STARE/ASHLAR manifest JSON for ``method in ("tiled", "ashlar")``
    (``bin/ashlar_solve.py`` deliberately rewrites ashlar's per-tile
    placements into STARE's own manifest format so one loader reads both), or
    a VALIS registrar pickle for ``method == "valis"``. ``method ==
    "identity"`` needs no ``transform_path`` at all: it is the do-nothing
    baseline (zero displacement everywhere), scored so every other method's
    absolute EPE has something to be compared against -- see the branch at
    the top of this function's body.

    When ``transform_path`` is omitted, the only OTHER method that may still
    be scored is ``"tiled"`` -- via the in-process ``run_stare`` driver the
    unit rung uses. Any competitor method with no transform raises: there is
    no in-process re-registration for a competitor backend, and silently
    falling back to STARE's own recomputation would score every method as
    STARE wearing another method's label -- the exact fabrication this
    parameter exists to make structurally impossible.

    Gate-ROC and STARE's own intrinsic TRE (``accepted``/``scores``/
    ``intrinsic``) come from ``run_stare``'s per-tile control-point JSONs,
    which are intermediate-only in the real pipeline (never published --
    ``TILED_REG_TILE``'s ``publishDir`` is explicitly disabled, and the
    published manifest carries only the solved M0 + mesh, not per-tile
    accept/reject decisions). So when a ``transform_path`` is given, this
    passes ``gate_stats=None``/``gate_auc_value=None`` to ``accuracy_row``
    rather than calling ``gate_roc``/``gate_auc`` on the empty ``accepted``/
    ``scores`` mappings -- those functions do not error and do not come back
    empty on real labels: ``gate_recall`` lands on a literal ``0.0`` (every
    truly-registrable tile scores as a false negative) and ``gate_auc`` on a
    literal ``0.5`` (its own documented tie-broken value for an
    uninformative constant score), two fabricated, plausible-looking
    numbers for a metric that was never actually measured. ``accuracy_row``
    writes ``None`` into every ``gate_*`` column for exactly this case.
    Likewise ``intrinsic`` stays ``None`` -- there is no ``*_tre.json`` to
    read outside the ``run_stare`` branch either.

    A pipeline change publishing the per-tile control-point JSONs (the data
    this gap is missing) has landed on ``feat/stare-ultimate`` (commit
    ``6823d4b``, at ``<outdir>/<pid>/registered/controls/*_ctrl.json``); once
    that reaches ``benchmarking``, this path can read them and stop passing
    ``None`` for the gate columns. Tracked here so the gap isn't forgotten.
    """
    pair_dir = Path(pair_dir)
    truth = json.loads((pair_dir / "truth.json").read_text())
    if int(tile) != int(truth["tile"]):
        raise ValueError(
            f"tile={tile} does not match this pair's generation tile "
            f"truth['tile']={truth['tile']}: truth['tile_labels'] is keyed to "
            "the GENERATION grid's (ix, iy), so scoring with a different "
            "registration tile would silently pair the gate-ROC decisions "
            "with unrelated tile-label cells."
        )
    shape = tuple(truth["size"])
    field = make_field(truth["field_family"], shape, **truth["field_params_call"])

    accepted = {}
    scores = {}
    intrinsic = None
    # Only run_stare's own per-tile control-point JSONs carry real gate data
    # (see the docstring above). A transform_path-based score has none, and
    # must say so with None rather than routing empty accepted/scores
    # mappings through gate_roc/gate_auc, which do not come back empty.
    have_gate_data = False

    if method == "identity":
        # The do-nothing baseline: zero displacement everywhere. No transform,
        # no run_stare -- this needs neither. It exists because an absolute
        # EPE number is uninterpretable on its own: FROZEN.md records STARE
        # scoring ratio(max) = 0.998 against this exact predictor at the unit
        # rung, so every other method's EPE must sit beside this row or a
        # true number reads as a false claim of accuracy.
        def predict(xy):
            return np.zeros_like(np.asarray(xy, dtype=float))
    elif transform_path is None:
        if method != "tiled":
            raise ValueError(
                f"score_pair(method={method!r}) has no transform_path: "
                "there is no in-process re-registration for a competitor "
                "method, so this would silently score STARE's own "
                "run_stare() re-registration under another method's label. "
                "Pass the real pipeline's transform via transform_path."
            )
        result = run_stare(pair_dir, work_dir, tile=tile, halo=halo,
                           upsample=upsample, max_error=max_error)
        predict = predict_from_manifest(result["manifest"])
        for c in result["controls"]:
            key = (int(c["ix"]), int(c["iy"]))
            scores[key] = float(c.get("error", float("nan")))
            accepted[key] = _reconstruct_accept(c, max_error, halo)
        have_gate_data = True
        if result["tre_json"] is not None:
            doc = json.loads(Path(result["tre_json"]).read_text())
            block = doc.get("rigid_tre_px")
            intrinsic = block.get("p50") if isinstance(block, dict) else None
    elif method in ("tiled", "ashlar"):
        predict = predict_from_manifest(transform_path)
    elif method == "valis":
        predict = _predict_from_valis_pickle(transform_path)
    else:
        raise ValueError(f"score_pair: unknown method {method!r}")

    gate_stats = gate_roc(truth["tile_labels"], accepted) if have_gate_data else None
    gate_auc_value = gate_auc(truth["tile_labels"], scores) if have_gate_data else None

    return accuracy_row(
        run_id=run_id, method=method, pair_id=pair_dir.name, truth=truth,
        epe_stats=epe(field, predict, shape, tile=min(tile, 512)),
        gate_stats=gate_stats,
        gate_auc_value=gate_auc_value,
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
    ap.add_argument("--transform", default=None,
                    help="already-produced transform from a real pipeline run: a "
                         "STARE/ASHLAR manifest JSON (--method tiled|ashlar) or a "
                         "VALIS registrar pickle (--method valis). Omit only for "
                         "--method tiled, which then falls back to the in-process "
                         "run_stare driver the unit rung uses.")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)

    row = score_pair(a.pair_dir, a.work_dir, method=a.method, run_id=a.run_id,
                     tile=a.tile, halo=a.halo, upsample=a.upsample,
                     max_error=a.max_error, transform_path=a.transform)
    write_accuracy_csv([row], a.out)
    print(f"Wrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
