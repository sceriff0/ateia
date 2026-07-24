#!/usr/bin/env python3
"""Evaluate one patient's cell segmentation with vendored CSE (2D)."""

import argparse
import json
import os
import sys

import numpy as np
import tifffile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "utils"))
from cse import single_method_eval  # noqa: E402


def _relabel_contiguous(mask):
    """Remap label image to contiguous 1..N (background 0 preserved).

    CSE's get_matched_masks indexes per-label coord lists by label value and
    assumes labels are contiguous; a gap would raise IndexError on a real mask.
    Metric values are label-agnostic, so this remap is safe.
    """
    uniq = np.unique(mask)
    # int32 (labels are small positive ints, far below 2^31) halves the transient
    # RAM of these full-WSI-sized label arrays vs int64; values are identical.
    lut = np.zeros(int(uniq.max()) + 1, dtype=np.int32)
    pos = uniq[uniq != 0]
    lut[pos] = np.arange(1, len(pos) + 1, dtype=np.int32)
    return lut[mask]


def _read_image_cyx(path):
    """Return (channels (C,Y,X), pixel_um_or_None). Prefer aicsimageio for
    robust OME axis handling (present in the container); fall back to tifffile."""
    try:
        from aicsimageio import AICSImage

        a = AICSImage(path)
        data = np.asarray(a.get_image_data("CYX"))  # T,Z are 1 for 2D WSI
        ps = a.physical_pixel_sizes
        px = float(ps.X) if (ps.X and ps.Y) else None
        return data, px
    except Exception:
        arr = np.asarray(tifffile.imread(path))
        if arr.ndim == 2:
            arr = arr[np.newaxis, :, :]
        elif arr.ndim == 3 and arr.shape[-1] <= 5 and arr.shape[0] > 5:
            arr = np.moveaxis(arr, -1, 0)  # YXC -> CYX heuristic
        return arr, None


def main():
    """CLI entry point: evaluate one patient's cell segmentation with vendored CSE and write the result."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--cell-mask", required=True)
    ap.add_argument("--nuclei-mask", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--id", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--pixel-size-um", default=None)
    a = ap.parse_args()

    channels, px_meta = _read_image_cyx(a.image)  # (C,Y,X)
    img_data = channels[np.newaxis, :, np.newaxis, :, :]  # (1,C,1,Y,X)
    # img["img"]=None forces CSE's metadata-free thresholding path, matching the
    # golden equivalence fixture exactly.
    img = {"name": a.id, "img": None, "data": img_data}

    cell = _relabel_contiguous(tifffile.imread(a.cell_mask).astype(np.int32))
    nuc = _relabel_contiguous(tifffile.imread(a.nuclei_mask).astype(np.int32))
    # CSE mask["data"] is (T,C,Z,Y,X) with C-axis: ch0=cell, ch1=nucleus.
    mask_data = np.stack([cell, nuc], 0)[np.newaxis, :, np.newaxis, :, :]  # (1,2,1,Y,X)
    mask = {"name": a.id, "img": None, "data": mask_data}

    if a.pixel_size_um:
        px = py = float(a.pixel_size_um)
    elif px_meta:
        px = py = px_meta
    else:
        raise SystemExit("Pixel size missing from image metadata; pass --pixel-size-um")

    metrics = single_method_eval(
        img, mask, PCA_model=False, output_dir=".", pixelsizex=px, pixelsizey=py
    )
    qs = metrics.get("QualityScore")
    doc = {
        "id": a.id,
        "metrics": metrics,
        "QualityScore": float(qs) if qs is not None else float("nan"),
    }
    with open(a.out, "w") as fh:
        json.dump(doc, fh, indent=2, default=lambda o: float(o))


if __name__ == "__main__":
    main()
