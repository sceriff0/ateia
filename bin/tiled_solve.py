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
from pathlib import Path

os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/numba_cache")
os.environ["NUMBA_DISABLE_CACHING"] = "1"

sys.path.insert(0, str(Path(__file__).parent / "utils"))

import numpy as np  # noqa: E402
from logger import configure_logging, get_logger  # noqa: E402
from tiled_manifest import build_manifest, slide_entry  # noqa: E402

logger = get_logger(__name__)


def _grid_from_controls(controls, gate_tre):
    nx = max(c["ix"] for c in controls) + 1
    ny = max(c["iy"] for c in controls) + 1
    grid_x = [0.0] * nx
    grid_y = [0.0] * ny
    disp = [[[0.0, 0.0] for _ in range(nx)] for _ in range(ny)]
    for c in controls:
        grid_x[c["ix"]] = float(c["cx"])
        grid_y[c["iy"]] = float(c["cy"])
        if float(c["tre"]) >= gate_tre:
            disp[c["iy"]][c["ix"]] = [float(c["dx"]), float(c["dy"])]
    return grid_x, grid_y, disp


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
    a = ap.parse_args(argv)

    m0_doc = json.loads(Path(a.m0).read_text())
    m0 = np.asarray(m0_doc["M0"], dtype=float)
    reference_name = a.reference_name or m0_doc.get("ref_name") or "reference"

    files = sorted(glob.glob(a.controls))
    if not files:
        raise FileNotFoundError(f"no control-point JSONs matched {a.controls!r}")
    controls = [json.loads(Path(f).read_text()) for f in files]

    grid_x, grid_y, disp = _grid_from_controls(controls, a.gate_tre)
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
    n_refined = sum(1 for row in disp for d in row if d != [0.0, 0.0])
    logger.info(
        f"solve: {len(controls)} tiles -> manifest (mesh={'yes' if entry['mesh'] else 'no'}, "
        f"{n_refined} tiles refined)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
