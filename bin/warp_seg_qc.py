#!/usr/bin/env python
"""GeoJSON/warp segmentation-overlap registration QC (reg_qc=2).

Each slide's nuclei are segmented on its NATIVE (pre-registration) image and exported
as a cell GeoJSON (see mask_to_geojson.py). This step warps those polygons through the
VALIS registrar and scores nuclei-mask overlap, before vs after registration:

  before = overlap of the raw native polygons (no registration)  — baseline misalignment
  after  = overlap of the polygons warped into the aligned frame — registered
  delta  = after - before                                        — registration improvement

Warping the exact polygons (not resampling a warped image) isolates registration quality
from segmentation-on-interpolated-pixels bias. Runs in the VALIS container (valis + numpy
+ scikit-image). The warp is lazy + injectable so the rasterize/score logic is unit-testable.

**Rasterization scale.** The polygons live in level-0 slide coordinates, so their bounding
grid is the whole slide — 10^9-10^10 px, which no label raster can hold. The warp itself is
done at full float precision (``pt_level=0``); only the *area integration* is quantized, by
scaling polygon coordinates by a single factor chosen to fit ``--max-raster-px``. That factor
is applied identically to ``before`` and ``after``, so the two stay directly comparable, and
it is recorded in the output next to the resulting median cell area so a reviewer can check
that cells stayed resolvable. Do **not** use VALIS's ``pt_level`` for this: it downsamples only
the warped side, silently making ``before`` and ``after`` incommensurable.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "utils"))

# Read-only $HOME on HPC clusters breaks numba's on-disk cache during the valis
# import (RuntimeError: '_repeat_1d': no locator available). Redirect + disable
# before importing valis (mirrors register.py / reg_finalize.py).
os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/numba_cache")
os.environ["NUMBA_DISABLE_CACHING"] = "1"
# matplotlib (pulled in transitively by valis) builds a font cache under $HOME by default; on a
# read-only cluster $HOME that warns/stalls. Redirect its config + generic XDG cache to /tmp too.
os.environ.setdefault("MPLCONFIGDIR", "/tmp/mplconfig")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/xdg_cache")

import numpy as np

# Rasterizing more than this many pixels is what got WARP_SEG_QC killed (exit 140) on
# whole-slide inputs. At ~10 bytes/px across the two uint32 label images and the metric's
# block transients, 400 Mpx is ~4 GB — comfortable inside the task's memory with the
# BioFormats JVM heap also resident.
DEFAULT_MAX_RASTER_PX = 400_000_000
# A nucleus below this many raster pixels can no longer support a stable IoU, so the
# resolvability flag trips and the run is marked as such rather than silently degraded.
DEFAULT_MIN_CELL_AREA_PX = 20.0
_AREA_SAMPLE = 5000  # features sampled when estimating median cell area


def _log(msg, t0=None):
    suffix = f" ({time.perf_counter() - t0:.1f}s)" if t0 is not None else ""
    print(f"[warp_seg_qc] {msg}{suffix}", flush=True)


# ── VALIS warp (lazy + injectable) ─────────────────────────────────────────────
def default_loader(pickle_path):
    from valis import registration  # lazy: real VALIS package, as in bin/register.py
    return registration.load_registrar(str(pickle_path))


def warp_cells(registrar, slide_name, geojson_f, pt_level=0, non_rigid=True, crop="overlap"):
    """Warp a slide's cell GeoJSON into the registered/aligned frame (FeatureCollection dict)."""
    slide = registrar.slide_dict[slide_name]
    return slide.warp_geojson(str(geojson_f), pt_level=pt_level,
                              non_rigid=non_rigid, crop=crop)


# ── polygon rasterization (skimage.draw.polygon — the standard rasterizer) ──────
def _iter_rings(geometry):
    """Yield ``(outer_ring, [hole_rings])`` per polygon in a GeoJSON geometry."""
    if not geometry:
        return
    gtype, coords = geometry.get("type"), geometry.get("coordinates")
    if gtype == "Polygon" and coords:
        yield coords[0], list(coords[1:])
    elif gtype == "MultiPolygon" and coords:
        for poly in coords:
            if poly:
                yield poly[0], list(poly[1:])


def _ring_pixels(ring, shape_rc, offset_xy=(0.0, 0.0), scale=1.0):
    from skimage.draw import polygon as sk_polygon
    pts = np.asarray(ring, dtype=float)
    if pts.shape[0] < 3:
        return (np.empty(0, dtype=np.intp), np.empty(0, dtype=np.intp))
    ox, oy = float(offset_xy[0]), float(offset_xy[1])
    h, w = int(shape_rc[0]), int(shape_rc[1])
    # Scale in polygon space, then shift into the grid. Identical treatment of the native
    # and warped collections is what keeps before/after comparable.
    return sk_polygon(pts[:, 1] * scale - oy, pts[:, 0] * scale - ox, shape=(h, w))


def rasterize_features(feature_collection, shape_rc, offset_xy=(0.0, 0.0), scale=1.0):
    """Rasterize a FeatureCollection to a uint32 label image (label = feature index).

    Interior rings (holes) are punched back out, but only where they fall on their own
    feature — a hole must never erase a neighbouring cell that overlaps it. Overlapping
    outer rings resolve last-wins; ``seg_overlap`` derives cell counts from the labels
    actually present, so a fully occluded cell is not counted as a missed match.
    """
    labels = np.zeros((int(shape_rc[0]), int(shape_rc[1])), dtype=np.uint32)
    for i, ft in enumerate(feature_collection.get("features", []), start=1):
        for outer, holes in _iter_rings(ft.get("geometry")):
            rr, cc = _ring_pixels(outer, shape_rc, offset_xy, scale)
            if rr.size:
                labels[rr, cc] = i
            for hole in holes:
                hr, hc = _ring_pixels(hole, shape_rc, offset_xy, scale)
                if hr.size:
                    own = labels[hr, hc] == i
                    labels[hr[own], hc[own]] = 0
    return labels


def common_grid(*feature_collections, pad=1, scale=1.0):
    """Bounding grid ``(shape_rc, offset_xy)`` covering all features at ``scale``.

    Running min/max rather than concatenating every ring: on a whole-slide collection the
    coordinate arrays alone are hundreds of MB and are needed only for their extent.
    """
    minx = miny = math.inf
    maxx = maxy = -math.inf
    for fc in feature_collections:
        for ft in fc.get("features", []):
            for outer, _holes in _iter_rings(ft.get("geometry")):
                arr = np.asarray(outer, dtype=float)
                if arr.size == 0:
                    continue
                minx = min(minx, float(arr[:, 0].min())); maxx = max(maxx, float(arr[:, 0].max()))
                miny = min(miny, float(arr[:, 1].min())); maxy = max(maxy, float(arr[:, 1].max()))
    if not math.isfinite(minx):
        return (1, 1), (0.0, 0.0)
    sminx = math.floor(minx * scale) - pad
    sminy = math.floor(miny * scale) - pad
    smaxx = math.ceil(maxx * scale) + pad
    smaxy = math.ceil(maxy * scale) + pad
    return (int(smaxy - sminy), int(smaxx - sminx)), (float(sminx), float(sminy))


def median_cell_area(*feature_collections, sample=_AREA_SAMPLE):
    """Median cell area in native px², estimated from outer-ring bounding boxes.

    Feeds only the resolvability guard, so an estimate suffices: nuclei are convex blobs
    and a blob fills ~pi/4 of its bounding box. Sampled, not exhaustive — a median over a
    few thousand cells is stable, and this must not become another O(cells) walk.
    """
    areas = []
    for fc in feature_collections:
        feats = fc.get("features", [])
        step = max(1, len(feats) // sample) if sample else 1
        for ft in feats[::step]:
            for outer, _holes in _iter_rings(ft.get("geometry")):
                arr = np.asarray(outer, dtype=float)
                if arr.shape[0] < 3:
                    continue
                bw = float(arr[:, 0].max() - arr[:, 0].min())
                bh = float(arr[:, 1].max() - arr[:, 1].min())
                areas.append(bw * bh * (math.pi / 4.0))
    return float(np.median(areas)) if areas else 0.0


def choose_raster_scale(shape_rc, cell_area_px, max_raster_px=DEFAULT_MAX_RASTER_PX,
                        min_cell_area_px=DEFAULT_MIN_CELL_AREA_PX) -> dict:
    """Pick the single rasterization scale (<=1) and report why.

    Memory is the binding constraint — the grid MUST fit — so the scale is set by
    ``max_raster_px`` alone. ``min_cell_area_px`` never relaxes it; it only decides the
    ``cells_resolvable`` flag, which is what makes a downsampled score defensible rather
    than silently degraded.
    """
    h, w = int(shape_rc[0]), int(shape_rc[1])
    n_px = max(1, h * w)
    scale = 1.0 if n_px <= max_raster_px else math.sqrt(max_raster_px / n_px)
    at_scale = float(cell_area_px) * scale * scale
    return {
        "scale": float(scale),
        "max_raster_px": int(max_raster_px),
        "native_grid_rc": [h, w],
        "native_grid_px": int(n_px),
        "scaled_grid_px": int(n_px * scale * scale),
        "median_cell_area_px": float(cell_area_px),
        "median_cell_area_px_at_scale": at_scale,
        "min_cell_area_px": float(min_cell_area_px),
        "cells_resolvable": bool(at_scale >= min_cell_area_px),
    }


def _score(fc_a, fc_b, iou_thresh=0.5, scale=1.0) -> dict:
    """Rasterize two FeatureCollections onto a common grid and score overlap."""
    from utils.seg_overlap import evaluate_masks
    shape_rc, offset = common_grid(fc_a, fc_b, scale=scale)
    a = rasterize_features(fc_a, shape_rc, offset, scale)
    b = rasterize_features(fc_b, shape_rc, offset, scale)
    out = evaluate_masks(a, b, iou_thresh=iou_thresh)
    out["grid_rc"] = [int(shape_rc[0]), int(shape_rc[1])]
    out["n_features_ref"] = len(fc_a.get("features", []))
    out["n_features_moving"] = len(fc_b.get("features", []))
    return out


def _load_fc(geojson_f) -> dict:
    with open(geojson_f) as f:
        return json.load(f)


# ── orchestration ──────────────────────────────────────────────────────────────
_DELTA_KEYS = ("dice", "iou", "instance_f1", "instance_precision", "instance_recall")


def run(pickle_path, ref_slide, moving_slide, ref_geojson, moving_geojson,
        loader=default_loader, pt_level=0, crop="overlap", iou_thresh=0.5,
        max_raster_px=DEFAULT_MAX_RASTER_PX,
        min_cell_area_px=DEFAULT_MIN_CELL_AREA_PX) -> dict:
    """Score nuclei-polygon overlap before (raw) vs after (warped) registration.

    Ordered so at most two FeatureCollections are resident at once: the natives are scored
    and dropped before the registrar is loaded and the warped pair is built.
    """
    t0 = time.perf_counter()
    ref_fc, mov_fc = _load_fc(ref_geojson), _load_fc(moving_geojson)
    n_ref_native, n_mov_native = len(ref_fc.get("features", [])), len(mov_fc.get("features", []))
    _log(f"loaded native geojsons: ref={n_ref_native} mov={n_mov_native} cells", t0)

    # One scale, chosen from the native geometry, applied to both scores. The warped frame
    # is the registration overlap crop, so it is normally smaller; the guard further down
    # catches the case where a non-rigid warp expands it anyway.
    native_grid, _ = common_grid(ref_fc, mov_fc)
    raster = choose_raster_scale(native_grid, median_cell_area(ref_fc, mov_fc),
                                 max_raster_px=max_raster_px, min_cell_area_px=min_cell_area_px)
    scale = raster["scale"]
    _log(f"native grid {native_grid[0]}x{native_grid[1]} px -> scale={scale:.4f} "
         f"(median cell {raster['median_cell_area_px']:.0f} -> "
         f"{raster['median_cell_area_px_at_scale']:.0f} px^2, "
         f"resolvable={raster['cells_resolvable']})", t0)
    if not raster["cells_resolvable"]:
        _log("WARNING: cells fall below the resolvable-area floor at this scale; metrics "
             "are still reported but flagged cells_resolvable=false")

    before = _score(ref_fc, mov_fc, iou_thresh=iou_thresh, scale=scale)
    _log(f"before: dice={before['dice']:.4f} f1={before['instance_f1']:.4f} "
         f"grid={before['grid_rc']}", t0)
    del ref_fc, mov_fc

    registrar = loader(pickle_path)
    _log("registrar loaded", t0)
    ref_w = warp_cells(registrar, ref_slide, ref_geojson, pt_level=pt_level, crop=crop)
    mov_w = warp_cells(registrar, moving_slide, moving_geojson, pt_level=pt_level, crop=crop)
    n_ref_warped, n_mov_warped = len(ref_w.get("features", [])), len(mov_w.get("features", []))
    _log(f"warped: ref={n_ref_warped} mov={n_mov_warped} cells", t0)

    # Guard: if the warped frame is unexpectedly larger than the native one, the shared
    # scale no longer fits the budget. Retighten and re-score `before` from disk rather
    # than let the raster blow past the memory the task was given.
    warped_grid, _ = common_grid(ref_w, mov_w, scale=scale)
    if warped_grid[0] * warped_grid[1] > max_raster_px:
        raster = choose_raster_scale(
            common_grid(ref_w, mov_w)[0], median_cell_area(ref_w, mov_w),
            max_raster_px=max_raster_px, min_cell_area_px=min_cell_area_px)
        scale = raster["scale"]
        raster["retightened_from_warped_frame"] = True
        _log(f"warped frame exceeded the budget at the native scale; retightened to "
             f"scale={scale:.4f} and re-scoring before", t0)
        before = _score(_load_fc(ref_geojson), _load_fc(moving_geojson),
                        iou_thresh=iou_thresh, scale=scale)

    after = _score(ref_w, mov_w, iou_thresh=iou_thresh, scale=scale)
    _log(f"after: dice={after['dice']:.4f} f1={after['instance_f1']:.4f} "
         f"grid={after['grid_rc']}", t0)

    delta = {k: after[k] - before[k] for k in _DELTA_KEYS}
    return {
        "before": before,
        "after": after,
        "delta": delta,
        "raster": raster,
        "counts": {
            "features_ref_native": n_ref_native,
            "features_moving_native": n_mov_native,
            "features_ref_warped": n_ref_warped,
            "features_moving_warped": n_mov_warped,
            # The warp is 1:1 per feature. If it is not, before and after describe
            # different cell populations and `delta` is no longer a pure registration
            # effect — surface that rather than quietly reporting the difference.
            "paired": bool(n_ref_warped == n_ref_native and n_mov_warped == n_mov_native),
        },
        "params": {
            "pt_level": pt_level, "crop": crop, "non_rigid": True,
            "iou_thresh": float(iou_thresh),
        },
    }


def build_record(result, patient_id, moving_name, reference_name) -> dict:
    return {
        "patient_id": patient_id,
        "moving": os.path.basename(str(moving_name)),
        "reference": os.path.basename(str(reference_name)),
        **result,
    }


def write_report(pickle_path, ref_slide, moving_slide, ref_geojson, moving_geojson, output,
                 patient_id=None, moving_name=None, reference_name=None,
                 loader=default_loader, pt_level=0, crop="overlap", iou_thresh=0.5,
                 max_raster_px=DEFAULT_MAX_RASTER_PX,
                 min_cell_area_px=DEFAULT_MIN_CELL_AREA_PX) -> dict:
    result = run(pickle_path, ref_slide, moving_slide, ref_geojson, moving_geojson,
                 loader=loader, pt_level=pt_level, crop=crop, iou_thresh=iou_thresh,
                 max_raster_px=max_raster_px, min_cell_area_px=min_cell_area_px)
    record = build_record(result, patient_id,
                          moving_name or moving_slide, reference_name or ref_slide)
    with open(output, "w") as f:
        json.dump(record, f, indent=2)
    return record


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="GeoJSON/warp segmentation-overlap registration QC.")
    ap.add_argument("--pickle", required=True, help="VALIS registrar pickle")
    ap.add_argument("--ref-slide", required=True, help="reference slide name in registrar.slide_dict")
    ap.add_argument("--moving-slide", required=True, help="moving slide name in registrar.slide_dict")
    ap.add_argument("--ref-geojson", required=True, help="reference native-cell GeoJSON")
    ap.add_argument("--moving-geojson", required=True, help="moving native-cell GeoJSON")
    ap.add_argument("--output", required=True)
    ap.add_argument("--patient-id", default=None)
    ap.add_argument("--moving-name", default=None, help="display filename for the moving slide")
    ap.add_argument("--reference-name", default=None, help="display filename for the reference slide")
    ap.add_argument("--pt-level", type=int, default=0,
                    help="VALIS warp level. Leave at 0: it downsamples only the warped side, "
                         "which breaks before/after comparability. Use --max-raster-px instead.")
    ap.add_argument("--crop", default="overlap")
    ap.add_argument("--iou-thresh", type=float, default=0.5)
    ap.add_argument("--max-raster-px", type=int, default=DEFAULT_MAX_RASTER_PX,
                    help="rasterization budget in pixels; sets the shared before/after scale")
    ap.add_argument("--min-cell-area-px", type=float, default=DEFAULT_MIN_CELL_AREA_PX,
                    help="median cell area below which the run is flagged not resolvable")
    ap.add_argument("--jvm-heap-gb", type=int, default=None,
                    help="explicit BioFormats JVM heap (GB); default = auto-size from the pickle dir")
    return ap.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)

    # Start the BioFormats JVM BEFORE any slide I/O. registration.load_registrar() unpickles VALIS
    # Slide objects whose BioFormats-backed readers reconstruct against a live JVM, and warp_geojson()
    # reads slide geometry — exactly like the register stages, which is why they all call init_jvm()
    # first. Without this, load_registrar() dies with jpype JVMNotRunning. WARP_SEG_QC stages no
    # TIFFs, so the auto-sizer always lands on its 8 GB floor; pass --jvm-heap-gb to make the
    # reservation explicit rather than incidental.
    from valis import registration
    from valis_config import init_jvm
    heap_gb = init_jvm(os.path.dirname(os.path.abspath(a.pickle)) or ".", override_gb=a.jvm_heap_gb)
    _log(f"started BioFormats JVM (heap={heap_gb}GB)")

    try:
        rec = write_report(a.pickle, a.ref_slide, a.moving_slide, a.ref_geojson, a.moving_geojson,
                           a.output, patient_id=a.patient_id, moving_name=a.moving_name,
                           reference_name=a.reference_name, pt_level=a.pt_level, crop=a.crop,
                           iou_thresh=a.iou_thresh, max_raster_px=a.max_raster_px,
                           min_cell_area_px=a.min_cell_area_px)
    finally:
        registration.kill_jvm()

    d = rec["delta"]
    print(f"Wrote {a.output}: after_dice={rec['after']['dice']:.4f} "
          f"before_dice={rec['before']['dice']:.4f} "
          f"delta_dice={d['dice']:+.4f} delta_f1={d['instance_f1']:+.4f} "
          f"scale={rec['raster']['scale']:.4f} paired={rec['counts']['paired']}")


if __name__ == "__main__":
    main()
