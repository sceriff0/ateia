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
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np


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
    if not geometry:
        return
    gtype, coords = geometry.get("type"), geometry.get("coordinates")
    if gtype == "Polygon" and coords:
        yield coords[0]
    elif gtype == "MultiPolygon" and coords:
        for poly in coords:
            if poly:
                yield poly[0]


def _ring_pixels(ring, shape_rc, offset_xy=(0.0, 0.0)):
    from skimage.draw import polygon as sk_polygon
    pts = np.asarray(ring, dtype=float)
    if pts.shape[0] < 3:
        return (np.empty(0, dtype=np.intp), np.empty(0, dtype=np.intp))
    ox, oy = float(offset_xy[0]), float(offset_xy[1])
    h, w = int(shape_rc[0]), int(shape_rc[1])
    return sk_polygon(pts[:, 1] - oy, pts[:, 0] - ox, shape=(h, w))  # (row=y, col=x)


def rasterize_features(feature_collection, shape_rc, offset_xy=(0.0, 0.0)):
    """Rasterize a FeatureCollection to a uint32 label image (label = feature index)."""
    labels = np.zeros((int(shape_rc[0]), int(shape_rc[1])), dtype=np.uint32)
    for i, ft in enumerate(feature_collection.get("features", []), start=1):
        for ring in _iter_rings(ft.get("geometry")):
            rr, cc = _ring_pixels(ring, shape_rc, offset_xy)
            labels[rr, cc] = i
    return labels


def common_grid(*feature_collections, pad=1):
    """Bounding grid (shape_rc, offset_xy) covering all features, with ``pad`` margin."""
    xs, ys = [], []
    for fc in feature_collections:
        for ft in fc.get("features", []):
            for ring in _iter_rings(ft.get("geometry")):
                arr = np.asarray(ring, dtype=float)
                xs.append(arr[:, 0]); ys.append(arr[:, 1])
    if not xs:
        return (1, 1), (0.0, 0.0)
    xall, yall = np.concatenate(xs), np.concatenate(ys)
    minx, miny = np.floor(xall.min()) - pad, np.floor(yall.min()) - pad
    maxx, maxy = np.ceil(xall.max()) + pad, np.ceil(yall.max()) + pad
    return (int(maxy - miny), int(maxx - minx)), (float(minx), float(miny))


def _score(fc_a, fc_b, iou_thresh=0.5) -> dict:
    """Rasterize two FeatureCollections onto a common grid and score overlap."""
    from utils.seg_overlap import evaluate_masks
    shape_rc, offset = common_grid(fc_a, fc_b)
    a = rasterize_features(fc_a, shape_rc, offset)
    b = rasterize_features(fc_b, shape_rc, offset)
    return evaluate_masks(a, b, iou_thresh=iou_thresh)


def _load_fc(geojson_f) -> dict:
    with open(geojson_f) as f:
        return json.load(f)


# ── orchestration ──────────────────────────────────────────────────────────────
_DELTA_KEYS = ("dice", "iou", "instance_f1")


def run(pickle_path, ref_slide, moving_slide, ref_geojson, moving_geojson,
        loader=default_loader, pt_level=0, crop="overlap", iou_thresh=0.5) -> dict:
    """Score nuclei-polygon overlap before (raw) vs after (warped) registration."""
    # BEFORE: raw native polygons overlaid, no registration (baseline misalignment).
    before = _score(_load_fc(ref_geojson), _load_fc(moving_geojson), iou_thresh=iou_thresh)
    # AFTER: warp both slides' polygons into the aligned frame via the registrar.
    registrar = loader(pickle_path)
    ref_w = warp_cells(registrar, ref_slide, ref_geojson, pt_level=pt_level, crop=crop)
    mov_w = warp_cells(registrar, moving_slide, moving_geojson, pt_level=pt_level, crop=crop)
    after = _score(ref_w, mov_w, iou_thresh=iou_thresh)
    delta = {k: after[k] - before[k] for k in _DELTA_KEYS}
    return {"before": before, "after": after, "delta": delta}


def build_record(result, patient_id, moving_name, reference_name) -> dict:
    return {
        "patient_id": patient_id,
        "moving": os.path.basename(str(moving_name)),
        "reference": os.path.basename(str(reference_name)),
        **result,
    }


def write_report(pickle_path, ref_slide, moving_slide, ref_geojson, moving_geojson, output,
                 patient_id=None, moving_name=None, reference_name=None,
                 loader=default_loader, pt_level=0, crop="overlap", iou_thresh=0.5) -> dict:
    result = run(pickle_path, ref_slide, moving_slide, ref_geojson, moving_geojson,
                 loader=loader, pt_level=pt_level, crop=crop, iou_thresh=iou_thresh)
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
    ap.add_argument("--pt-level", type=int, default=0)
    ap.add_argument("--crop", default="overlap")
    ap.add_argument("--iou-thresh", type=float, default=0.5)
    return ap.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    rec = write_report(a.pickle, a.ref_slide, a.moving_slide, a.ref_geojson, a.moving_geojson,
                       a.output, patient_id=a.patient_id, moving_name=a.moving_name,
                       reference_name=a.reference_name, pt_level=a.pt_level, crop=a.crop,
                       iou_thresh=a.iou_thresh)
    d = rec["delta"]
    print(f"Wrote {a.output}: after_dice={rec['after']['dice']:.4f} "
          f"before_dice={rec['before']['dice']:.4f} "
          f"delta_dice={d['dice']:+.4f} delta_f1={d['instance_f1']:+.4f}")


if __name__ == "__main__":
    main()
