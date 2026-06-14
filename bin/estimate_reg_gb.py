#!/usr/bin/env python3
"""Estimate VALIS's non-rigid RAM (`est_GB`) for a patient's slides, to decide tiling.

Replicates VALIS 1.0.0's own tiling decision (registration.py:3455-3462): non-rigid registration
tiles iff `est_GB > TILER_THRESH_GB` (10). We reproduce the *exact* formula (including VALIS's
`calc_memory_size_gb` bitdepth quirk) so the Nextflow router matches what classic VALIS would do:

  est_GB = n_slides * (img_gb + displacement_gb + processed_img_gb)   [all at the non-rigid resolution]
  calc_memory_size_gb(shape, nch, dtype) = (nch * H * W * 8 / bitdepth) / 2**30

So routing is: est_GB > threshold  -> distributed tiling (== classic-tiled regime)
               est_GB <= threshold -> classic whole-image REGISTER (identical to classic)

The non-rigid shape is approximated by scaling the reference's native dims to `max_non_rigid_dim`
(VALIS uses the post-rigid *aligned* stack shape, which we can't know pre-rigid; this is close and
only matters near the boundary). Metadata is read with tifffile (no JVM) for speed.

Prints `est_gb=<float> use_tiler=<true|false>` and writes `<out>` JSON.
"""
import argparse
import json
import re

import numpy as np
import tifffile

TILER_THRESH_GB = 10  # VALIS registration.TILER_THRESH_GB


def calc_memory_size_gb(shape_hw, nchannels, np_dtype):
    """Verbatim port of valis.warp_tools.calc_memory_size_gb (quirk included)."""
    bits = "".join(re.findall(r"\d+", np_dtype))
    bitdepth = eval(bits) if bits else 1
    n_px = nchannels * int(shape_hw[0]) * int(shape_hw[1])
    return ((n_px * 8) / bitdepth) / (2 ** 30)


def read_meta(path):
    """(H, W, n_channels, numpy-dtype-name) from an OME/TIFF without a JVM."""
    with tifffile.TiffFile(path) as tf:
        s = tf.series[0]
        shape, axes = list(s.shape), s.axes
        dtype = np.dtype(s.dtype).name
    dim = {ax: shape[i] for i, ax in enumerate(axes)}
    h = dim.get("Y", shape[-2])
    w = dim.get("X", shape[-1])
    nch = dim.get("C", 1)
    return int(h), int(w), int(nch), dtype


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reference", required=True, help="reference image path")
    ap.add_argument("--images", nargs="+", required=True, help="all patient image paths (incl. reference)")
    ap.add_argument("--max-non-rigid-dim", type=int, default=4096)
    ap.add_argument("--threshold-gb", type=float, default=TILER_THRESH_GB)
    ap.add_argument("--out", default="est_gb.json")
    args = ap.parse_args()

    n_slides = len(args.images)
    rh, rw, rnch, rdtype = read_meta(args.reference)

    # non-rigid shape: scale the reference to max_non_rigid_dim (downscale only; matches VALIS which
    # runs non-rigid at <= max_non_rigid_registration_dim_px).
    scale = min(1.0, args.max_non_rigid_dim / float(max(rh, rw)))
    nr_shape = (int(round(rh * scale)), int(round(rw * scale)))

    displacement_gb = n_slides * calc_memory_size_gb(nr_shape, 2, "float32")
    processed_img_gb = n_slides * calc_memory_size_gb(nr_shape, 1, "uint8")
    img_gb = n_slides * calc_memory_size_gb(nr_shape, rnch, rdtype)
    est_gb = img_gb + displacement_gb + processed_img_gb
    use_tiler = est_gb > args.threshold_gb

    result = {
        "est_gb": est_gb, "use_tiler": use_tiler, "threshold_gb": args.threshold_gb,
        "n_slides": n_slides, "nr_shape_hw": list(nr_shape), "ref_native_hw": [rh, rw],
        "ref_n_channels": rnch, "ref_dtype": rdtype, "max_non_rigid_dim": args.max_non_rigid_dim,
        "components_gb": {"img": img_gb, "displacement": displacement_gb, "processed": processed_img_gb},
    }
    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=2)
    print(f"est_gb={est_gb:.4f} use_tiler={str(use_tiler).lower()} "
          f"(threshold={args.threshold_gb}, n_slides={n_slides}, nr_shape={nr_shape})", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
