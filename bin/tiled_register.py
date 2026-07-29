#!/usr/bin/env python3
"""STARE tiled registration CLI — register one moving slide to the reference.

Wraps ``utils.tiled_pipeline.register_slide``: estimates a global rigid ``M0`` from the DAPI
channels, refines it with a TRE-gated mesh built from per-tile phase correlation, and warps every
channel of the moving slide into the reference frame with **bilinear** resampling (non-negative by
construction). Writes the registered OME-TIFF, the transform manifest (consumed by the reg_qc=2
warper), and a per-slide TRE summary.

JVM-free: pure NumPy + scikit-image + tifffile — the property that makes it a laptop/low-end
method, unlike the VALIS path.

Usage::

    tiled_register.py --reference ref.ome.tiff --moving mov.ome.tiff \\
        --dapi-index 0 --out mov_registered.ome.tiff \\
        --manifest manifest.json --tre-summary tre.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np

# numba/matplotlib caches under a read-only $HOME crash on HPC nodes — redirect before imports
# that might trigger them (mirrors register.py / warp_seg_qc.py).
os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/numba_cache")
os.environ["NUMBA_DISABLE_CACHING"] = "1"
os.environ.setdefault("MPLCONFIGDIR", "/tmp/mplconfig")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/xdg_cache")

sys.path.insert(0, str(Path(__file__).parent / "utils"))

import tifffile  # noqa: E402
from logger import configure_logging, get_logger  # noqa: E402
from tiled_pipeline import register_slide  # noqa: E402
from tre_report import build_tre_report  # noqa: E402

logger = get_logger(__name__)


def _load_channels(path):
    """Read an OME-TIFF as a ``(C, H, W)`` float32 array (single-channel promoted to C=1)."""
    arr = tifffile.imread(str(path))
    arr = np.asarray(arr)
    if arr.ndim == 2:
        arr = arr[np.newaxis, ...]
    elif arr.ndim != 3:
        raise ValueError(
            f"{path}: expected a 2-D or 3-D (C,H,W) image, got shape {arr.shape}"
        )
    return arr


def run(args) -> int:
    ref = _load_channels(args.reference)
    mov = _load_channels(args.moving)
    logger.info(f"reference {ref.shape}, moving {mov.shape}")

    if not (0 <= args.dapi_index < ref.shape[0] and args.dapi_index < mov.shape[0]):
        raise ValueError(
            f"--dapi-index {args.dapi_index} out of range for ref C={ref.shape[0]}, "
            f"mov C={mov.shape[0]}"
        )

    ref_dapi = ref[args.dapi_index].astype(np.float32)
    mov_dapi = mov[args.dapi_index].astype(np.float32)
    # warp_image works on (H, W, C); move channels last for the moving slide
    mov_hwc = np.moveaxis(mov, 0, -1).astype(np.float32)

    result = register_slide(
        ref_dapi,
        mov_dapi,
        tile=args.tile,
        halo=args.halo,
        gate_tre=args.gate_tre,
        upsample=args.upsample,
        warp_data=mov_hwc,
        out_shape=ref_dapi.shape,
    )

    # (H, W, C) -> (C, H, W); clamp the FP rounding floor to guarantee non-negativity, then restore
    # the source dtype so downstream quantification sees the same integer range it expects.
    registered = np.moveaxis(result["registered"], -1, 0)
    registered = np.clip(registered, 0.0, None)
    src_dtype = tifffile.imread(str(args.moving)).dtype
    if np.issubdtype(src_dtype, np.integer):
        info = np.iinfo(src_dtype)
        registered = np.clip(np.rint(registered), info.min, info.max)
    registered = registered.astype(src_dtype)

    tifffile.imwrite(str(args.out), registered, photometric="minisblack")
    logger.info(f"wrote registered slide {registered.shape} -> {args.out}")

    # Include the reference as an identity entry so the manifest is self-contained for the
    # reg_qc=2 warper, which warps both the reference and the moving cells through it.
    manifest = {
        "ref_slide": args.reference_name,
        "slides": {
            args.reference_name: {
                "M0": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
                "mesh": None,
            },
            args.moving_name: result["entry"],
        },
    }
    Path(args.manifest).write_text(json.dumps(manifest, indent=2))

    records = [
        {
            "ix": t.ix,
            "iy": t.iy,
            "cx": t.cx,
            "cy": t.cy,
            "tre_rigid": float(pre),
            "tre_after": float(post),
        }
        for t, pre, post in zip(
            result["tiles"], result["tre_px"], result["tre_after_px"]
        )
    ]
    summary = build_tre_report(
        result["coarse_tre"],
        result["n_inliers"],
        records,
        result["entry"]["mesh"] is not None,
    )
    summary["moving"] = args.moving_name
    summary["reference"] = args.reference_name
    Path(args.tre_summary).write_text(json.dumps(summary, indent=2))
    final = summary.get("residual_after_px", {}).get("p50")
    logger.info(
        f"TRE: coarse={summary['coarse_tre_px']:.2f}px, rigid p50={summary['rigid_tre_px']['p50']}, "
        f"final p50={final}, refined={summary['mesh_refined']}"
    )
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="STARE tiled registration of one moving slide."
    )
    p.add_argument("--reference", required=True, help="reference OME-TIFF")
    p.add_argument("--moving", required=True, help="moving OME-TIFF")
    p.add_argument("--out", required=True, help="output registered OME-TIFF")
    p.add_argument("--manifest", required=True, help="output transform manifest JSON")
    p.add_argument("--tre-summary", required=True, help="output TRE summary JSON")
    p.add_argument(
        "--dapi-index", type=int, default=0, help="DAPI channel index for estimation"
    )
    p.add_argument("--tile", type=int, default=2048, help="tile core size (px)")
    p.add_argument("--halo", type=int, default=256, help="tile read halo (px)")
    p.add_argument(
        "--gate-tre",
        type=float,
        default=1.0,
        help="refine only tiles whose rigid-stage TRE (px) meets this threshold",
    )
    p.add_argument(
        "--upsample", type=int, default=10, help="phase-correlation upsample factor"
    )
    p.add_argument(
        "--reference-name", default=None, help="reference slide name for the manifest"
    )
    p.add_argument(
        "--moving-name", default=None, help="moving slide name for the manifest"
    )
    a = p.parse_args(argv)
    a.reference_name = a.reference_name or Path(a.reference).name
    a.moving_name = a.moving_name or Path(a.moving).name
    return a


def main(argv=None) -> int:
    configure_logging()
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
