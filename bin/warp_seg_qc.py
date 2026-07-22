#!/usr/bin/env python
"""Staged, fixed-correspondence segmentation-overlap registration QC (reg_qc=2).

Each slide's nuclei are segmented on its NATIVE (pre-registration) image and exported as a cell
GeoJSON (see ``segment_to_geojson.py``). This step warps those polygons through the VALIS
registrar **once per registration stage** and reports how the same cells behave at each:

  native      no transform                          — the raw stage misalignment
  rigid       rigid only (post micro-rigid M)       — **the anchor**
  non_rigid   rigid + wave-1 non-rigid field        — needs the REGISTER pre-micro checkpoint
  micro       rigid + non-rigid + micro residual    — what the pipeline actually ships

**Correspondence is established once, at the rigid stage, and then held fixed.** Two things
make that the right anchor. Cells are close enough after rigid for nearest-neighbour pairing to
be meaningful — before any registration they are a whole tissue-offset apart, and any matcher
run there is pairing noise. And because ``MicroRigidRegistrar`` runs inside ``register()``
*before* the non-rigid stage, the rigid transform the anchor uses is the very transform every
later stage builds on; the pairing is therefore valid for all of them by construction.

Holding the pairing is what makes the per-stage numbers mean something. Re-deriving the match
at each stage — what this script used to do — lets a score move because *different cells got
paired*, or because a cell the rasterizer occluded at one stage reappeared at another. Neither
is a registration effect. With the pairing fixed, every change between stages is geometry.

Two metrics per stage, both over the fixed pairs:

  * **per-pair IoU** — mean/percentiles and the fraction at IoU >= threshold. Each pair is
    rasterized in its own bounding-box window, so peak RAM is one nucleus rather than one
    slide. This replaces the whole-slide uint32 label images the previous implementation
    needed (19 GB on a 60k x 40k slide) and with them the raster-budget machinery.
  * **centroid residual displacement** — median/p90/max distance between paired centroids, in
    px and, where the slide metadata carries a pixel size, in µm. It has no resolution floor
    the way a thresholded IoU does, and "residual error is 1.4 µm after non-rigid" is a
    number that can be quoted.

Deltas are reported against the anchor, so ``delta_vs_anchor.micro.displacement_px_p50`` reads
directly as "what the non-rigid and micro stages bought over rigid alone" — and, when it has
the wrong sign, as a warning that a stage made things worse.

Runs in the VALIS container (valis + numpy + scipy + scikit-image). The warp is injectable so
the staging, matching and scoring logic is unit-testable without VALIS.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "utils"))

# Read-only $HOME on HPC clusters breaks numba's on-disk cache during the valis import
# (RuntimeError: '_repeat_1d': no locator available). Redirect + disable before importing valis
# (mirrors register.py / reg_finalize.py).
os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/numba_cache")
os.environ["NUMBA_DISABLE_CACHING"] = "1"
# matplotlib (pulled in transitively by valis) builds a font cache under $HOME by default; on a
# read-only cluster $HOME that warns/stalls. Redirect its config + generic XDG cache to /tmp too.
os.environ.setdefault("MPLCONFIGDIR", "/tmp/mplconfig")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/xdg_cache")

import numpy as np

from utils import cell_pairs as cp
from utils.valis_stage_warp import (
    STAGE_MICRO,
    STAGE_NATIVE,
    STAGE_NON_RIGID,
    STAGE_RIGID,
)

ANCHOR_STAGE = STAGE_RIGID
# Pair two cells only if each is the other's nearest centroid AND they are within this many
# median nuclear radii. 1.0 is deliberately tight: after a rigid alignment a correctly matched
# nucleus sits well inside one radius, and anything further is more likely a neighbour than a
# partner. Loosening it trades precision for recall in the pairing, not in the registration.
DEFAULT_MATCH_RADIUS_FACTOR = 1.0
DEFAULT_SUPERSAMPLE = 2
DEFAULT_IOU_THRESH = 0.5
# Deltas are reported only for these — the headline numbers. Counts and percentile widths are
# all in the per-stage records; the delta of a count is not a meaningful quantity.
_DELTA_KEYS = ("iou_mean", "iou_p50", "displacement_px_p50", "displacement_px_p90",
               "displacement_um_p50", "displacement_um_p90", "dice_matched")


def _log(msg, t0=None):
    suffix = f" ({time.perf_counter() - t0:.1f}s)" if t0 is not None else ""
    print(f"[warp_seg_qc] {msg}{suffix}", flush=True)


# ── VALIS access (lazy + injectable) ───────────────────────────────────────────
def default_loader(pickle_path):
    from valis import registration  # lazy: real VALIS package, as in bin/register.py
    return registration.load_registrar(str(pickle_path))


# ── stage planning ─────────────────────────────────────────────────────────────
def plan_stages(checkpoint=None) -> tuple:
    """Decide which transform states this run can actually distinguish.

    Returns ``(stages, separable, note)``. There are three cases, and conflating them is how a
    QC report ends up quietly reporting the same warp twice under two names:

    * checkpoint present, micro-registration ran — all four stages are distinct.
    * checkpoint present, micro-registration was skipped — the pickle's field *is* the wave-1
      field, so ``micro`` would duplicate ``non_rigid``; it is not emitted.
    * no checkpoint — the pickle's field already has the micro residual composed in and the
      intermediate is unrecoverable, so ``non_rigid`` is not emitted.
    """
    if checkpoint is None:
        return ([STAGE_NATIVE, STAGE_RIGID, STAGE_MICRO], False,
                "no REGISTER stage checkpoint: the registrar's field already has the micro "
                "residual composed in, so the 'non_rigid' stage cannot be separated from it")
    if not checkpoint.micro_registration:
        return ([STAGE_NATIVE, STAGE_RIGID, STAGE_NON_RIGID], True,
                "micro-registration did not run: the final field is the wave-1 non-rigid "
                "field, so there is no distinct 'micro' stage")
    return ([STAGE_NATIVE, STAGE_RIGID, STAGE_NON_RIGID, STAGE_MICRO], True, "")


# ── one stage's geometry ───────────────────────────────────────────────────────
def stage_geometry(warp, slide_name, native_ps, stage):
    """Warp a native :class:`PolySet` into ``stage``'s frame and measure it.

    The returned set shares its ring bookkeeping with ``native_ps``, so feature index ``i`` is
    the same cell at every stage — the invariant the fixed pairing depends on.
    """
    warped = native_ps.with_xy(warp(slide_name, native_ps.xy, stage))
    area, cent = cp.feature_area_centroid(warped)
    return warped, area, cent


def score_stage(ps_ref, ps_mov, cent_ref, cent_mov, area_ref, area_mov, idx_ref, idx_mov,
                iou_thresh=DEFAULT_IOU_THRESH, supersample=DEFAULT_SUPERSAMPLE,
                max_pair_window_px=cp.DEFAULT_MAX_PAIR_WINDOW_PX, pixel_size_um=None) -> dict:
    """Per-pair IoU + centroid residual for one stage, over an already-fixed pairing."""
    iou, scored = cp.pair_iou(ps_ref, ps_mov, idx_ref, idx_mov,
                              supersample=supersample, max_window_px=max_pair_window_px)
    dist = cp.centroid_distance(cent_ref, cent_mov, idx_ref, idx_mov)
    return cp.summarize_stage(iou, scored, dist,
                              np.asarray(area_ref)[idx_ref], np.asarray(area_mov)[idx_mov],
                              iou_thresh=iou_thresh, pixel_size_um=pixel_size_um)


def _stage_line(rec) -> str:
    def g(k, fmt=".4f"):
        v = rec.get(k)
        return format(v, fmt) if isinstance(v, float) else "n/a"
    return (f"pairs={rec.get('n_pairs_scored', 0)}/{rec.get('n_pairs', 0)} "
            f"iou_mean={g('iou_mean')} iou_p50={g('iou_p50')} "
            f"disp_p50={g('displacement_px_p50', '.2f')}px "
            f"disp_p90={g('displacement_px_p90', '.2f')}px")


# ── orchestration ──────────────────────────────────────────────────────────────
def run(ref_geojson, moving_geojson, warp, stages, pixel_size_um=None,
        ref_slide=None, moving_slide=None,
        match_radius_factor=DEFAULT_MATCH_RADIUS_FACTOR, match_radius_px=None,
        iou_thresh=DEFAULT_IOU_THRESH, supersample=DEFAULT_SUPERSAMPLE,
        max_pair_window_px=cp.DEFAULT_MAX_PAIR_WINDOW_PX) -> dict:
    """Score every stage in ``stages`` against a correspondence fixed at the anchor.

    The anchor is computed first regardless of where it sits in the reporting order, and each
    remaining stage is warped, scored and dropped before the next is built — so peak memory is
    the two native vertex arrays plus one stage's, not one array per stage.
    """
    t0 = time.perf_counter()
    with open(ref_geojson) as f:
        ref_native = cp.from_feature_collection(json.load(f))
    with open(moving_geojson) as f:
        mov_native = cp.from_feature_collection(json.load(f))
    _log(f"loaded native geojsons: ref={ref_native.n_features} "
         f"mov={mov_native.n_features} cells", t0)

    if ANCHOR_STAGE not in stages:
        raise ValueError(f"anchor stage {ANCHOR_STAGE!r} is not in the stage plan {stages}")

    # ── anchor: warp, size the match radius from the cells themselves, pair once ──
    a_ref, a_area_ref, a_cent_ref = stage_geometry(warp, ref_slide, ref_native, ANCHOR_STAGE)
    a_mov, a_area_mov, a_cent_mov = stage_geometry(warp, moving_slide, mov_native, ANCHOR_STAGE)
    cell_radius = cp.median_equivalent_radius(np.concatenate([a_area_ref, a_area_mov]))
    radius = float(match_radius_px) if match_radius_px else float(match_radius_factor) * cell_radius
    idx_ref, idx_mov, _ = cp.match_mutual_nn(a_cent_ref, a_cent_mov, radius)
    _log(f"anchor '{ANCHOR_STAGE}': median cell radius {cell_radius:.2f} px -> match radius "
         f"{radius:.2f} px -> {idx_ref.size} pairs", t0)
    if idx_ref.size == 0:
        _log("WARNING: no cells paired at the anchor. Every stage record will be empty; this "
             "usually means the rigid stage failed or the two slides share no tissue.")

    denom = min(ref_native.n_features, mov_native.n_features) or 1
    matching = {
        "method": "mutual_nn_centroid",
        "anchor_stage": ANCHOR_STAGE,
        "radius_px": radius,
        "radius_factor": None if match_radius_px else float(match_radius_factor),
        "median_cell_radius_px": cell_radius,
        "n_pairs": int(idx_ref.size),
        "pair_fraction": float(idx_ref.size) / denom,
        "pair_fraction_ref": float(idx_ref.size) / (ref_native.n_features or 1),
        "pair_fraction_moving": float(idx_ref.size) / (mov_native.n_features or 1),
    }

    score_kwargs = dict(iou_thresh=iou_thresh, supersample=supersample,
                        max_pair_window_px=max_pair_window_px, pixel_size_um=pixel_size_um)
    records = {ANCHOR_STAGE: score_stage(a_ref, a_mov, a_cent_ref, a_cent_mov,
                                         a_area_ref, a_area_mov, idx_ref, idx_mov,
                                         **score_kwargs)}
    _log(f"stage '{ANCHOR_STAGE}': {_stage_line(records[ANCHOR_STAGE])}", t0)
    del a_ref, a_mov, a_cent_ref, a_cent_mov, a_area_ref, a_area_mov

    for stage in stages:
        if stage == ANCHOR_STAGE:
            continue
        s_ref, ar_ref, c_ref = stage_geometry(warp, ref_slide, ref_native, stage)
        s_mov, ar_mov, c_mov = stage_geometry(warp, moving_slide, mov_native, stage)
        records[stage] = score_stage(s_ref, s_mov, c_ref, c_mov, ar_ref, ar_mov,
                                     idx_ref, idx_mov, **score_kwargs)
        _log(f"stage '{stage}': {_stage_line(records[stage])}", t0)
        del s_ref, s_mov, c_ref, c_mov, ar_ref, ar_mov

    anchor_rec = records[ANCHOR_STAGE]
    deltas = {
        stage: {k: records[stage][k] - anchor_rec[k]
                for k in _DELTA_KEYS if k in records[stage] and k in anchor_rec}
        for stage in stages if stage != ANCHOR_STAGE
    }

    return {
        "stage_order": list(stages),
        "stages": records,
        "delta_vs_anchor": deltas,
        "matching": matching,
        "counts": {
            "features_ref": int(ref_native.n_features),
            "features_moving": int(mov_native.n_features),
        },
        "params": {
            "iou_thresh": float(iou_thresh),
            "supersample": int(supersample),
            "max_pair_window_px": int(max_pair_window_px),
            "pixel_size_um": pixel_size_um,
        },
    }


def build_record(result, patient_id, moving_name, reference_name, separable, note) -> dict:
    rec = {
        "patient_id": patient_id,
        "moving": os.path.basename(str(moving_name)),
        "reference": os.path.basename(str(reference_name)),
        "stages_separable": bool(separable),
        **result,
    }
    if note:
        rec["note"] = note
    return rec


def write_report(pickle_path, ref_slide, moving_slide, ref_geojson, moving_geojson, output,
                 patient_id=None, moving_name=None, reference_name=None,
                 checkpoint_dir=None, loader=default_loader, crop="overlap", clip=False,
                 pixel_size_um=None, warp=None, stages=None, separable=True, note="",
                 **run_kwargs) -> dict:
    """Load the registrar (unless a warp is injected), score every stage, write the JSON."""
    if warp is None:
        from utils.stage_checkpoint import StageCheckpoint
        from utils.valis_stage_warp import make_warper, slide_pixel_size_um

        checkpoint = StageCheckpoint.load(checkpoint_dir) if checkpoint_dir else None
        if checkpoint is not None and not checkpoint.has_slide(moving_slide):
            raise KeyError(
                f"the stage checkpoint has no entry for moving slide {moving_slide!r}; it has "
                f"{checkpoint.slide_names}. The checkpoint and the registrar must come from "
                "the same REGISTER task.")
        stages, separable, note = plan_stages(checkpoint)
        registrar = loader(pickle_path)
        _log("registrar loaded")
        warp = make_warper(registrar, crop=crop, clip=clip, checkpoint=checkpoint)
        if pixel_size_um is None:
            pixel_size_um = slide_pixel_size_um(registrar, moving_slide)
    elif stages is None:
        stages, separable, note = plan_stages(None)
    _log(f"stage plan: {stages} (separable={separable})" + (f" — {note}" if note else ""))

    result = run(ref_geojson, moving_geojson, warp, stages, pixel_size_um=pixel_size_um,
                 ref_slide=ref_slide, moving_slide=moving_slide, **run_kwargs)
    record = build_record(result, patient_id, moving_name or moving_slide,
                          reference_name or ref_slide, separable, note)
    with open(output, "w") as f:
        json.dump(record, f, indent=2)
    return record


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Staged, fixed-correspondence segmentation-overlap registration QC.")
    ap.add_argument("--pickle", required=True, help="VALIS registrar pickle")
    ap.add_argument("--ref-slide", required=True, help="reference slide name in registrar.slide_dict")
    ap.add_argument("--moving-slide", required=True, help="moving slide name in registrar.slide_dict")
    ap.add_argument("--ref-geojson", required=True, help="reference native-cell GeoJSON")
    ap.add_argument("--moving-geojson", required=True, help="moving native-cell GeoJSON")
    ap.add_argument("--output", required=True)
    ap.add_argument("--checkpoint-dir", default=None,
                    help="REGISTER pre-micro stage checkpoint. Without it the 'non_rigid' stage "
                         "cannot be separated from 'micro' and is not reported.")
    ap.add_argument("--patient-id", default=None)
    ap.add_argument("--moving-name", default=None, help="display filename for the moving slide")
    ap.add_argument("--reference-name", default=None, help="display filename for the reference slide")
    ap.add_argument("--crop", default="overlap")
    ap.add_argument("--clip-to-frame", action="store_true",
                    help="reproduce warp_geojson's clip of warped vertices to the aligned frame. "
                         "Off by default: it flattens boundary-straddling cells onto the crop "
                         "edge in both slides, inflating their IoU for non-registration reasons.")
    ap.add_argument("--match-radius-factor", type=float, default=DEFAULT_MATCH_RADIUS_FACTOR,
                    help="match radius in median nuclear radii (default 1.0)")
    ap.add_argument("--match-radius-px", type=float, default=None,
                    help="absolute match radius in px; overrides --match-radius-factor")
    ap.add_argument("--iou-thresh", type=float, default=DEFAULT_IOU_THRESH,
                    help="IoU at which a pair counts as 'well aligned' for frac_iou_ge_*")
    ap.add_argument("--supersample", type=int, default=DEFAULT_SUPERSAMPLE,
                    help="per-pair rasterization factor. 2 puts the IoU quantum near 1%% for a "
                         "typical nucleus; the window is per-pair, so the cost is negligible.")
    ap.add_argument("--max-pair-window-px", type=int, default=cp.DEFAULT_MAX_PAIR_WINDOW_PX,
                    help="a pair spanning a larger window is counted but not scored")
    ap.add_argument("--pixel-size-um", type=float, default=None,
                    help="physical pixel size; default = read from the slide metadata")
    ap.add_argument("--jvm-heap-gb", type=int, default=None,
                    help="explicit BioFormats JVM heap (GB); default = auto-size from the pickle dir")
    return ap.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)

    # Start the BioFormats JVM BEFORE any slide I/O. registration.load_registrar() unpickles VALIS
    # Slide objects whose BioFormats-backed readers reconstruct against a live JVM, and the warp
    # parameters read slide geometry — exactly like the register stages, which is why they all call
    # init_jvm() first. Without this, load_registrar() dies with jpype JVMNotRunning. WARP_SEG_QC
    # stages no TIFFs, so the auto-sizer always lands on its 8 GB floor; pass --jvm-heap-gb to make
    # the reservation explicit rather than incidental.
    from valis import registration
    from valis_config import init_jvm
    heap_gb = init_jvm(os.path.dirname(os.path.abspath(a.pickle)) or ".", override_gb=a.jvm_heap_gb)
    _log(f"started BioFormats JVM (heap={heap_gb}GB)")

    try:
        rec = write_report(
            a.pickle, a.ref_slide, a.moving_slide, a.ref_geojson, a.moving_geojson, a.output,
            patient_id=a.patient_id, moving_name=a.moving_name, reference_name=a.reference_name,
            checkpoint_dir=a.checkpoint_dir, crop=a.crop, clip=a.clip_to_frame,
            pixel_size_um=a.pixel_size_um,
            match_radius_factor=a.match_radius_factor, match_radius_px=a.match_radius_px,
            iou_thresh=a.iou_thresh, supersample=a.supersample,
            max_pair_window_px=a.max_pair_window_px)
    finally:
        registration.kill_jvm()

    nan = float("nan")
    last = rec["stage_order"][-1]
    anchor, final = rec["stages"][ANCHOR_STAGE], rec["stages"][last]
    delta = rec["delta_vs_anchor"].get(last, {})
    print(f"Wrote {a.output}: pairs={rec['matching']['n_pairs']} "
          f"({rec['matching']['pair_fraction']:.1%} of cells) | "
          f"{ANCHOR_STAGE}: iou={anchor.get('iou_mean', nan):.4f} "
          f"disp_p50={anchor.get('displacement_px_p50', nan):.2f}px | "
          f"{last}: iou={final.get('iou_mean', nan):.4f} "
          f"disp_p50={final.get('displacement_px_p50', nan):.2f}px | "
          f"delta_iou={delta.get('iou_mean', nan):+.4f} "
          f"delta_disp_p50={delta.get('displacement_px_p50', nan):+.2f}px")


if __name__ == "__main__":
    main()
