#!/usr/bin/env python3
"""Extract the mask series (Image:1) from a mask-carrying pyramid OME-TIFF.

Writes cell_mask.tif and nuclei_mask.tif (uint32). Fast-fails with a clear
message when the pyramid has no second series or it is not a 2-channel uint32
mask series (the incremental cyclic-IF mode relies on this).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import tifffile

sys.path.insert(0, str(Path(__file__).parent / "utils"))
from image_io import write_mask_tiff  # noqa: E402


def main() -> None:
    """CLI entry point: extract the mask series from a pyramid OME-TIFF into cell/nuclei mask TIFFs."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--pyramid", type=Path, required=True)
    ap.add_argument("--outdir", type=Path, required=True)
    args = ap.parse_args()

    with tifffile.TiffFile(args.pyramid) as tif:
        if len(tif.series) < 2:
            sys.exit(
                f"ERROR: {args.pyramid} has no mask series (found {len(tif.series)} series). "
                f"The prior run must set embed_masks=true with expanded compartment quantification."
            )
        # out="memmap" rather than a plain asarray: this is a full-resolution
        # 2-channel uint32 mask series -- the same read segment.py already does
        # lazily (`tif.asarray(out="memmap")`). Everything below touches one
        # plane at a time, so nothing needs both channels resident at once.
        # The writes stay INSIDE this `with` block so the mapping is still
        # backed by an open file when the planes are consumed.
        masks = tif.series[1].asarray(out="memmap")

        if masks.ndim != 3 or masks.shape[0] != 2:
            sys.exit(
                f"ERROR: mask series has shape {masks.shape}; expected (2, H, W) [cell, nuclei]."
            )
        # Fast-fail on dtypes that cannot losslessly represent uint32 label IDs.
        # Accept unsigned-integer masks (uint8/uint16/uint32 -> lossless upcast) and
        # reject anything else (float probability maps, signed/int64 arrays) loudly
        # rather than silently corrupting downstream quantification.
        if masks.dtype not in (np.uint8, np.uint16, np.uint32):
            sys.exit(
                f"ERROR: mask series in {args.pyramid} has dtype {masks.dtype}; expected an unsigned "
                f"integer label mask (uint8/uint16/uint32). Refusing to coerce (silent-corruption risk)."
            )

        args.outdir.mkdir(parents=True, exist_ok=True)
        # Per-plane upcast, not `masks.astype(uint32)` on the whole series: the
        # cast is what materialises the data, so doing it a plane at a time keeps
        # peak memory at one mask instead of two.
        for plane, name in ((0, "cell_mask.tif"), (1, "nuclei_mask.tif")):
            data = masks[plane]
            if data.dtype != np.uint32:
                data = data.astype(np.uint32)  # lossless upcast from uint8/uint16
            # write_mask_tiff, not a bare imwrite: these were the only writes in
            # the pipeline with neither bigtiff nor compression, so an
            # uncompressed uint32 mask hit classic TIFF's 4 GB offset limit at a
            # smaller slide size than anything else here.
            write_mask_tiff(args.outdir / name, data)

        shape = masks.shape

    print(
        f"Extracted cell_mask + nuclei_mask from {args.pyramid} (series 1, {shape[1]}x{shape[2]})"
    )


if __name__ == "__main__":
    main()
