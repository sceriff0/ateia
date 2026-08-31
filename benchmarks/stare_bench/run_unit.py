"""Unit-rung STARE driver: the four stages, in sequence, in one process.

WHY NOT RESTORE bin/tiled_register.py: it exists on no branch -- it was the
single-task entry point removed when STARE was split into the four-stage
fan-out. Restoring it would create a SECOND code path that can silently drift
from the pipeline. This driver instead calls the same bin/tiled_*.py entry
points the Nextflow modules call, so there is nothing to drift.

(The one caller that still invoked it was the ANHIR/ACROBAT landmark harness,
benchmarks/registration_eval/. Its STARE leg had been broken since the split,
and the harness has now been deleted for that reason among others.)

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

__all__ = ["run_stare", "predict_from_manifest", "moving_slide_name", "REPO_ROOT"]

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
    pair_dir, work_dir, *, tile, halo, upsample, max_error, max_dim,
    extra_args=None,
):
    """COARSE -> REG_TILE (per tile) -> SOLVE, returning the manifest + controls.

    ``extra_args`` is forwarded to ``tiled_coarse.py`` ONLY -- it never reaches
    ``tiled_reg_tile.py``. This matters for ``--nuclear-index``/``--dapi-index``
    in particular: passing it through ``extra_args`` would move which channel
    COARSE estimates the anchor from, while every per-tile REG_TILE call would
    silently keep reading channel 0 (its own default). That produces a
    channel-mismatched registration with no error raised anywhere. Nothing in
    this driver does this today, but a future caller adding a nuclear-index
    override here needs to also thread it through the REG_TILE loop below.

    ``max_dim`` is REQUIRED and has deliberately no default here, mirroring
    ``bin/tiled_coarse.py --max-dim``: it is the resolution COARSE solves the
    global transform at, the dominant runtime-and-accuracy knob, and it is
    owned by ``RegPresets.STARE[<mode>].coarse_max_dim``. A default in this
    driver would be a second home for that value that nothing pins, and a
    stale one would silently score STARE at a resolution the pipeline never
    uses.
    """
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
                "--max-dim", max_dim,
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


def moving_slide_name(manifest):
    """The manifest's single non-reference slide key.

    Real STARE/ASHLAR manifests name the moving slide after
    ``meta.channels.join('_')`` (``tiled_solve.nf:29``, ``ashlar_solve.nf:51``),
    NOT the literal ``"mov"`` this benchmark's own in-process ``run_stare``
    driver happens to pass to ``tiled_solve.py --moving-name``. Deriving the
    name from the manifest itself (rather than hardcoding ``"mov"``) keeps
    scoring correct regardless of what channel name a real samplesheet uses --
    a real mid-rung manifest built from a ``channels = DAPI`` samplesheet is
    keyed ``"DAPI"``, and ``bin/utils/tiled_stage_warp.py``'s ``affines[slide_name]``
    raises ``KeyError`` on any other guess.
    """
    ref = manifest["ref_slide"]
    candidates = [k for k in manifest["slides"] if k != ref]
    if len(candidates) != 1:
        raise ValueError(
            "expected exactly one non-reference slide in manifest, found "
            f"{candidates!r} (ref_slide={ref!r}, slides={list(manifest['slides'])!r})"
        )
    return candidates[0]


def predict_from_manifest(manifest_path):
    """A ``(N, 2) -> (N, 2)`` displacement predictor for the FINAL stage.

    Built through bin/utils/tiled_stage_warp.make_warper -- the same builder the
    pipeline's own reg_qc=2 uses -- so the metrics score exactly the transform
    that would ship. The moving slide name is derived from the manifest itself
    (see ``moving_slide_name``), never hardcoded.
    """
    utils_dir = str(BIN / "utils")
    if utils_dir not in sys.path:
        sys.path.insert(0, utils_dir)
    from tiled_stage_warp import make_warper

    manifest = json.loads(Path(manifest_path).read_text())
    warp = make_warper(manifest)
    moving_name = moving_slide_name(manifest)

    def predict(xy):
        import numpy as np

        xy = np.asarray(xy, dtype=float)
        return np.asarray(warp(moving_name, xy, "refined"), dtype=float) - xy

    return predict
