#!/usr/bin/env python3
"""Registration accuracy benchmark CLI — method-agnostic.

Scores how well a registered slide aligns to the reference (residual feature TRE + intensity
correlation) with no ground truth, so it works on real WSIs. Run it on each method's registered
output to compare — e.g. VALIS vs the tiled (STARE) method on the same slide:

    registration_benchmark.py --reference ref.ome.tiff --registered valis_out.ome.tiff --output valis.json
    registration_benchmark.py --reference ref.ome.tiff --registered tiled_out.ome.tiff --output tiled.json

Optionally pass --moving to also report the before-registration residual (the improvement).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np

os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/numba_cache")
os.environ["NUMBA_DISABLE_CACHING"] = "1"
os.environ.setdefault("MPLCONFIGDIR", "/tmp/mplconfig")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/xdg_cache")

sys.path.insert(0, str(Path(__file__).parent / "utils"))

import tifffile  # noqa: E402
from logger import configure_logging, get_logger  # noqa: E402
from reg_benchmark import benchmark, feature_residual  # noqa: E402

logger = get_logger(__name__)


def _dapi(path, index):
    arr = np.asarray(tifffile.imread(str(path)))
    if arr.ndim == 2:
        return arr.astype(np.float32)
    if arr.ndim == 3:
        return arr[index].astype(np.float32)
    raise ValueError(f"{path}: expected 2-D or 3-D (C,H,W), got {arr.shape}")


def main(argv=None) -> int:
    configure_logging()
    ap = argparse.ArgumentParser(description="Registration accuracy benchmark.")
    ap.add_argument("--reference", required=True)
    ap.add_argument("--registered", required=True)
    ap.add_argument(
        "--moving", default=None, help="optional: also report the before residual"
    )
    ap.add_argument("--dapi-index", type=int, default=0)
    ap.add_argument("--output", required=True, help="output metrics JSON")
    ap.add_argument(
        "--method", default=None, help="label recorded in the report (e.g. valis/tiled)"
    )
    a = ap.parse_args(argv)

    ref = _dapi(a.reference, a.dapi_index)
    reg = _dapi(a.registered, a.dapi_index)

    if a.moving:
        report = benchmark(ref, _dapi(a.moving, a.dapi_index), reg)
    else:
        report = {"after": feature_residual(ref, reg)}
    report["method"] = a.method
    report["reference"] = Path(a.reference).name
    report["registered"] = Path(a.registered).name

    Path(a.output).write_text(json.dumps(report, indent=2))
    after = report["after"]
    logger.info(
        f"{a.method or 'registration'}: residual TRE median={after['tre_px_median']:.2f}px "
        f"p90={after['tre_px_p90']:.2f}px over {after['n_matches']} matches -> {a.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
