#!/usr/bin/env python3
"""Evaluate one patient's cell segmentation with vendored CSE (2D)."""

import argparse
import json
import logging

import numpy as np
import tifffile

# `from utils.cse`, not a sys.path insert + `import cse`: bin/ is already sys.path[0]
# for a script run from it, and this is the convention every other bin script uses
# (bin/warp_seg_qc.py: `from utils.valis_stage_warp import ...`). It also lets
# tests/test_container_harmonisation.py follow the import as LOCAL rather than
# reporting `cse` as a third-party package the image fails to install.
from utils.cse import single_method_eval

logger = logging.getLogger(__name__)


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


def _downsample_factor(shape_yx, max_pixels):
    """Smallest integer factor f>=1 such that the binned grid fits max_pixels.

    Returns 1 when downsampling is disabled (max_pixels falsy) or the image is
    already within budget. The binned grid is ceil(Y/f) x ceil(X/f).
    """
    import math

    y, x = shape_yx
    if not max_pixels or y * x <= max_pixels:
        return 1
    f = int(math.isqrt((y * x) // int(max_pixels))) or 1
    # isqrt can undershoot by one bin; step up until genuinely under budget.
    while ((y + f - 1) // f) * ((x + f - 1) // f) > max_pixels:
        f += 1
    return f


def _downsample(channels, cell, nuc, factor):
    """Bin the intensity image and label masks by `factor` on a shared grid.

    The image is mean-pooled (intensity is averageable); label masks are
    subsampled (`[::f, ::f]`) since label IDs are categorical. Both reductions
    key off the same block origin, so pixel<->label correspondence is preserved.
    `factor <= 1` is an exact no-op. Returns (channels, cell, nuc).
    """
    if factor <= 1:
        return channels, cell, nuc
    from skimage.measure import block_reduce

    ch = block_reduce(channels, (1, factor, factor), func=np.mean).astype(
        channels.dtype, copy=False
    )
    return ch, cell[::factor, ::factor], nuc[::factor, ::factor]


def _read_image_cyx(path):
    """Return (channels (C,Y,X), pixel_um_or_None) via tifffile.

    NO bioio/aicsimageio here, deliberately. The only thing a richer reader added
    was the physical pixel size -- and the pixel size is supplied by the caller:
    conf/modules.config always passes --pixel-size-um (from cse_pixel_size_um, else
    params.pixel_size), and main() prefers that over anything read from metadata.
    So the dependency bought a value that is always overridden.

    It also could not be installed: bioio-ome-tiff caps tifffile below the version
    tests/test_container_harmonisation.py harmonises on, so pinning both made
    containers/segeval unbuildable (ResolutionImpossible). Reading the array with
    tifffile and taking the pixel size from the CLI removes the conflict rather
    than adding a documented exception for a value nothing reads.

    Standalone invocation therefore requires --pixel-size-um; main() says so.
    """
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
    ap.add_argument(
        "--downsample",
        type=int,
        default=1,
        help="Bin image+masks by this integer factor before scoring (1 = off). "
        "Overrides --max-pixels when > 1.",
    )
    ap.add_argument(
        "--max-pixels",
        type=float,
        default=None,
        help="Auto-bin so the scored grid has at most this many pixels. Caps "
        "CSE's memory/time on full-WSI masks. Ignored if --downsample > 1.",
    )
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    channels, px_meta = _read_image_cyx(a.image)  # (C,Y,X)
    # .squeeze() drops a leading singleton axis: some label TIFFs are written
    # as (1,Y,X) rather than (Y,X), which would otherwise break the `y, x =
    # shape` unpack in _downsample_factor. A legitimate (Y,X) mask is a no-op.
    cell = tifffile.imread(a.cell_mask).astype(np.int32).squeeze()
    nuc = tifffile.imread(a.nuclei_mask).astype(np.int32).squeeze()

    if a.pixel_size_um:
        px = py = float(a.pixel_size_um)
    elif px_meta:
        px = py = px_meta
    else:
        raise SystemExit(
            "Pixel size is required: pass --pixel-size-um. This script does not read "
            "it from image metadata (see _read_image_cyx); the pipeline supplies it "
            "from params.cse_pixel_size_um or params.pixel_size.")

    # Downsample before scoring: full-WSI label masks otherwise blow CSE's
    # memory/time budget. Binning by `factor` means each pixel now spans
    # `factor`x more microns per axis, so scale the pixel size to match.
    factor = a.downsample if a.downsample > 1 else _downsample_factor(cell.shape, a.max_pixels)
    channels, cell, nuc = _downsample(channels, cell, nuc, factor)
    px *= factor
    py *= factor
    logger.info(
        f"[{a.id}] downsample_factor={factor} effective_pixel_size_um=({px:.4f}, {py:.4f})"
    )

    img_data = channels[np.newaxis, :, np.newaxis, :, :]  # (1,C,1,Y,X)
    # img["img"]=None forces CSE's metadata-free thresholding path, matching the
    # golden equivalence fixture exactly.
    img = {"name": a.id, "img": None, "data": img_data}

    # Relabel AFTER downsampling: subsampling can drop tiny cells and leave gaps
    # in the label sequence, which CSE's get_matched_masks would index past.
    cell = _relabel_contiguous(cell)
    nuc = _relabel_contiguous(nuc)
    # CSE mask["data"] is (T,C,Z,Y,X) with C-axis: ch0=cell, ch1=nucleus.
    mask_data = np.stack([cell, nuc], 0)[np.newaxis, :, np.newaxis, :, :]  # (1,2,1,Y,X)
    mask = {"name": a.id, "img": None, "data": mask_data}

    metrics = single_method_eval(
        img, mask, PCA_model=False, output_dir=".", pixelsizex=px, pixelsizey=py
    )
    qs = metrics.get("QualityScore")
    doc = {
        "id": a.id,
        "metrics": metrics,
        "QualityScore": float(qs) if qs is not None else float("nan"),
        "downsample_factor": factor,
        "effective_pixel_size_um": px,
    }
    with open(a.out, "w") as fh:
        json.dump(doc, fh, indent=2, default=lambda o: float(o))


if __name__ == "__main__":
    main()
