#!/usr/bin/env python
"""Convert a StarDist label mask (segment.py's ``*_cell_mask.tif``) into a cell
GeoJSON that ``warp_seg_qc.py`` / VALIS ``Slide.warp_geojson`` can consume.

Geometry-only (no marker intensities): one Polygon feature per label, contours
traced with the same crop -> find_contours -> simplify approach as
``bin/extract_cell_properties.py``. Runs in the StarDist/segmentation container
(scipy + scikit-image present).
"""

from __future__ import annotations

import argparse
import json

import numpy as np


def mask_to_feature_collection(mask, simplify_tolerance: float = 0.5) -> dict:
    """Trace each label's outer contour into a GeoJSON FeatureCollection (pixel x,y)."""
    from scipy import ndimage as ndi
    from skimage.measure import approximate_polygon, find_contours

    mask = np.asarray(mask)
    features = []
    for i, sl in enumerate(ndi.find_objects(mask), start=1):
        if sl is None:
            continue
        minr, minc = sl[0].start, sl[1].start
        crop = np.pad((mask[sl] == i).astype(np.uint8), 1, mode="constant")
        contours = find_contours(crop, level=0.5)
        if not contours:
            continue
        contour = max(contours, key=len)
        simp = approximate_polygon(contour, tolerance=simplify_tolerance)
        ring = [[float(c - 1 + minc), float(r - 1 + minr)] for r, c in simp]
        if len(ring) < 3:
            continue
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        features.append(
            {
                "type": "Feature",
                "properties": {"label": int(i)},
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
        )
    return {"type": "FeatureCollection", "features": features}


def write_geojson(mask_path, out_path, simplify_tolerance: float = 0.5) -> int:
    """Read a label-mask TIFF, convert, write GeoJSON. Returns the cell count."""
    import tifffile

    fc = mask_to_feature_collection(
        tifffile.imread(str(mask_path)), simplify_tolerance=simplify_tolerance
    )
    with open(out_path, "w") as f:
        json.dump(fc, f)
    return len(fc["features"])


def main():
    ap = argparse.ArgumentParser(description="StarDist cell-mask TIFF -> cell GeoJSON.")
    ap.add_argument("--mask", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--tolerance", type=float, default=0.5)
    a = ap.parse_args()
    n = write_geojson(a.mask, a.out, simplify_tolerance=a.tolerance)
    print(f"Wrote {n} cells to {a.out}")


if __name__ == "__main__":
    main()
