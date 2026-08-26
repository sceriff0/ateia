"""Unit-rung STARE driver: the four stages, in sequence, in one process.

WHY NOT RESTORE bin/tiled_register.py: registration_eval/run_registration.sh
still invokes it, but it exists on no branch -- it was the single-task entry
point removed when STARE was split into the four-stage fan-out. Restoring it
would create a SECOND code path that can silently drift from the pipeline. This
driver instead calls the same bin/tiled_*.py entry points the Nextflow modules
call, so there is nothing to drift.

The mid and gigapixel rungs do NOT use this driver -- they run the real
pipeline through the arm plan, because the <=8 GB and fan-out claims only mean
something under real resource limits.

Flag note: COARSE writes a tile-plan CSV (``--out-tiles``), not a JSON plan --
there is no ``--out-plan``. Its columns are exactly
``ix,iy,cx,cy,x0,y0,x1,y1,rx0,ry0,rx1,ry1``, where ``x0..y1`` is the tile core
and ``rx0..ry1`` is the read window (core plus halo). REG_TILE takes that read
window explicitly per tile (``--rx0/--ry0/--rx1/--ry1``) and does NOT accept
``--tile``/``--halo`` -- those two only shape the grid COARSE emits.
"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

from .metrics.cost import measure

__all__ = ["run_stare", "predict_from_manifest", "REPO_ROOT"]

REPO_ROOT = Path(__file__).resolve().parents[2]
BIN = REPO_ROOT / "bin"


def _run(script, args):
    cmd = [sys.executable, str(BIN / script), *[str(a) for a in args]]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{script} failed ({proc.returncode}):\n{proc.stderr}")
    return proc


def _read_tile_plan(csv_path):
    with open(csv_path, newline="") as f:
        return list(csv.DictReader(f))


def run_stare(
    pair_dir, work_dir, *, tile, halo, upsample, max_error, extra_args=None
):
    """COARSE -> REG_TILE (per tile) -> SOLVE, returning the manifest + controls."""
    pair_dir, work_dir = Path(pair_dir), Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    ref = pair_dir / "ref.ome.tiff"
    mov = pair_dir / "mov.ome.tiff"
    m0 = work_dir / "m0.json"
    tiles_csv = work_dir / "tiles.csv"
    extra = list(extra_args or [])

    def _all():
        _run(
            "tiled_coarse.py",
            [
                "--reference", ref, "--moving", mov,
                "--tile", tile, "--halo", halo,
                "--out-m0", m0, "--out-tiles", tiles_csv,
                *extra,
            ],
        )
        tiles = _read_tile_plan(tiles_csv)
        controls_dir = work_dir / "controls"
        controls_dir.mkdir(exist_ok=True)
        for t in tiles:
            ix, iy = t["ix"], t["iy"]
            out = controls_dir / f"c_{ix}_{iy}.json"
            _run(
                "tiled_reg_tile.py",
                [
                    "--reference", ref, "--moving", mov, "--m0", m0,
                    "--ix", ix, "--iy", iy,
                    "--cx", t["cx"], "--cy", t["cy"],
                    "--rx0", t["rx0"], "--ry0", t["ry0"],
                    "--rx1", t["rx1"], "--ry1", t["ry1"],
                    "--upsample", upsample,
                    "--out", out,
                ],
            )
        manifest = work_dir / "manifest.json"
        tre_json = work_dir / "tre.json"
        _run(
            "tiled_solve.py",
            [
                "--m0", m0,
                "--controls", str(controls_dir / "*.json"),
                "--max-error", max_error,
                "--max-disp", halo,
                "--moving-name", "mov",
                "--out-manifest", manifest,
                "--out-tre", tre_json,
            ],
        )
        return manifest, controls_dir, tre_json

    (manifest, controls_dir, tre_json), cost = measure(_all)
    controls = [
        json.loads(p.read_text()) for p in sorted(controls_dir.glob("*.json"))
    ]
    return {
        "manifest": manifest,
        "controls": controls,
        "tre_json": tre_json if tre_json.exists() else None,
        "cost": cost,
    }


def predict_from_manifest(manifest_path, moving_name):
    """A ``(N, 2) -> (N, 2)`` displacement predictor for the FINAL stage.

    Built through bin/utils/tiled_stage_warp.make_warper -- the same builder the
    pipeline's own reg_qc=2 uses -- so the metrics score exactly the transform
    that would ship.
    """
    sys.path.insert(0, str(BIN / "utils"))
    from tiled_stage_warp import make_warper

    warp = make_warper(json.loads(Path(manifest_path).read_text()))

    def predict(xy):
        import numpy as np

        xy = np.asarray(xy, dtype=float)
        return np.asarray(warp(moving_name, xy, "refined"), dtype=float) - xy

    return predict
