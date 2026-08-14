#!/usr/bin/env python3
"""STARE fan-out step 3/4: assemble the transform manifest from per-tile control points.

Gathers the control points emitted by every tile task, lays them on the grid with TRE gating, and
writes the self-contained manifest (reference identity + the moving slide's M0 + mesh) the warp and
the reg_qc=2 scorer consume. One cheap per-slide reduction — kilobytes, no image data.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import namedtuple
from pathlib import Path

os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/numba_cache")
os.environ["NUMBA_DISABLE_CACHING"] = "1"

sys.path.insert(0, str(Path(__file__).parent / "utils"))

import numpy as np  # noqa: E402
from logger import configure_logging, get_logger  # noqa: E402
from tiled_manifest import (  # noqa: E402
    assemble_control_grid,
    build_manifest,
    slide_entry,
)
from tre_report import build_tre_report  # noqa: E402

logger = get_logger(__name__)


# The gating rule itself lives in tiled_manifest.assemble_control_grid, which takes
# (tile, residual) pairs -- the in-memory shape the monolithic path already has. This
# reduction starts from serialized per-tile JSON instead, so all it has to supply is the
# grid-position half of a tile. That is the ONLY reason a local adapter exists here; the
# `tre >= gate_tre` decision is NOT restated (it used to be, in a hand-maintained copy that
# could drift from the shared one in either direction).
_ControlPoint = namedtuple("_ControlPoint", "ix iy cx cy")


def _as_tiles_and_residuals(controls):
    """Split control-point dicts into the (grid position, residual) pair the shared gate takes."""
    tiles = [
        _ControlPoint(int(c["ix"]), int(c["iy"]), float(c["cx"]), float(c["cy"]))
        for c in controls
    ]
    residuals = [(float(c["dx"]), float(c["dy"]), float(c["tre"])) for c in controls]
    return tiles, residuals


def main(argv=None) -> int:
    configure_logging()
    ap = argparse.ArgumentParser(
        description="STARE manifest assembly from control points."
    )
    ap.add_argument("--m0", required=True, help="M0 JSON from tiled_coarse")
    ap.add_argument(
        "--controls", required=True, help="glob for the per-tile control JSONs"
    )
    ap.add_argument("--gate-tre", type=float, default=1.0)
    ap.add_argument(
        "--reference-name",
        default=None,
        help="reference slide name for the manifest; defaults to ref_name from the M0 JSON",
    )
    ap.add_argument("--moving-name", required=True)
    ap.add_argument("--out-manifest", required=True)
    ap.add_argument(
        "--out-tre",
        default=None,
        help="output intrinsic-TRE summary JSON (rigid-stage spatial TRE from the control points)",
    )
    a = ap.parse_args(argv)

    m0_doc = json.loads(Path(a.m0).read_text())
    m0 = np.asarray(m0_doc["M0"], dtype=float)
    reference_name = a.reference_name or m0_doc.get("ref_name") or "reference"

    files = sorted(glob.glob(a.controls))
    if not files:
        raise FileNotFoundError(f"no control-point JSONs matched {a.controls!r}")
    controls = [json.loads(Path(f).read_text()) for f in files]

    grid_x, grid_y, disp = assemble_control_grid(
        *_as_tiles_and_residuals(controls), a.gate_tre
    )
    entry = slide_entry(m0, grid_x, grid_y, disp)
    # carry the reference frame so the stitch knows the output size without re-reading the reference
    entry["out_shape"] = [int(m0_doc["ref_h"]), int(m0_doc["ref_w"])]

    manifest = build_manifest(
        reference_name,
        {
            reference_name: {
                "M0": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
                "mesh": None,
            },
            a.moving_name: entry,
        },
    )
    Path(a.out_manifest).write_text(json.dumps(manifest, indent=2))

    # Intrinsic TRE (fix: the fan-out used to drop this). Built from the same per-tile phase
    # correlations the registration used — coarse rigid TRE + a spatial per-tile heatmap. The
    # post-refinement residual is not measured here (no re-warp in this reduction); the default
    # monolithic path and the reg_benchmark harness provide the final-accuracy number.
    if a.out_tre:
        records = [
            {
                "ix": int(c["ix"]),
                "iy": int(c["iy"]),
                "cx": float(c["cx"]),
                "cy": float(c["cy"]),
                "tre_rigid": float(c["tre"]),
            }
            for c in controls
        ]
        report = build_tre_report(
            m0_doc.get("coarse_tre", 0.0),
            m0_doc.get("n_inliers", 0),
            records,
            entry["mesh"] is not None,
        )
        report["moving"] = a.moving_name
        report["reference"] = reference_name
        report["note"] = (
            "fan-out: rigid-stage spatial TRE; post-refinement residual not measured here"
        )
        Path(a.out_tre).write_text(json.dumps(report, indent=2))

    n_refined = sum(1 for row in disp for d in row if d != [0.0, 0.0])
    logger.info(
        f"solve: {len(controls)} tiles -> manifest (mesh={'yes' if entry['mesh'] else 'no'}, "
        f"{n_refined} tiles refined)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
