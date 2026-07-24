#!/usr/bin/env python3
"""Task 10 verification — distributed tiled registration is bit-identical to VALIS's in-process tiler.

Runs inside bolt3x/attend_image_analysis:mirage_valis_1.0.0. Proves the make-or-break end to end on the P001 fluorescence pair:

  distributed:  REG_PREP -> (per-tile, fresh process) reg_tile.py -> REG_FINALIZE   ==
  baseline:     the SAME PREP-dumped 2-D images run through VALIS's OWN in-process
                NonRigidTileRegistrar, then the SAME compose+pad+warp (reg_finalize logic)

If the warped output pixels are identical (max|Δ|=0), the process-level fan-out is a faithful
decomposition of VALIS's tiler. (Per-leg proofs: §6.1 tile kernel, §6.3 compose+warp; this ties them
together through the full pipeline.) The baseline is the in-process TILER on the processed 2-D images
— NOT whole-image classic, which is a different algorithm (§6.5).

Usage:
  docker run --rm -v "$PWD":/work -w /work bolt3x/attend_image_analysis:mirage_valis_1.0.0 \
      python3 tests/integration/verify_distributed_bitidentical.py
"""

import json
import os
import shutil
import subprocess
import sys

import numpy as np
import pyvips

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin")
)
import reg_finalize  # reuse the EXACT compose + pad + warp logic (also puts bin/utils on sys.path)
from mirage_slide_reader import open_multiband  # noqa: E402
from valis import slide_tools, warp_tools
from valis.non_rigid_registrars import NonRigidTileRegistrar, OpticalFlowWarper

WORK = "/tmp/verify_dist"
INP = os.path.join(WORK, "in")
PREP = os.path.join(WORK, "prep")
TILE_WH, TILE_BUFFER = 48, 16  # small => multi-tile grid (exercises the stitch seam)


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write("\n".join((r.stdout + r.stderr).splitlines()[-12:]))
        raise SystemExit(f"FAILED: {' '.join(cmd[1:4])}")


def main():
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(INP)
    for s in ("P001_ref.ome.tiff", "P001_mov1.ome.tiff"):
        shutil.copy(os.path.join("tests/testdata", s), os.path.join(INP, s))

    # --- distributed path: PREP -> reg_tile (per tile, fresh proc) -> FINALIZE ---
    run(
        [
            sys.executable,
            "bin/reg_prep.py",
            "--input-dir",
            INP,
            "--out",
            PREP,
            "--reference",
            "P001_ref.ome.tiff",
            "--tile-wh",
            str(TILE_WH),
            "--tile-buffer",
            str(TILE_BUFFER),
            "--memory-mode",
            "low",
        ]
    )
    md = os.path.join(PREP, "P001_mov1")
    ti = os.path.join(md, "tiler_inputs")
    tiles = os.path.join(md, "tiles")
    os.makedirs(tiles, exist_ok=True)
    manifest = json.load(open(os.path.join(ti, "manifest.json")))
    n = manifest["n_tiles"]
    print(
        f"[verify] grid n_tiles={n} ({manifest['n_rows']}x{manifest['n_cols']})",
        flush=True,
    )
    for i in range(n):
        run(
            [
                sys.executable,
                "bin/reg_tile.py",
                "--inputs-dir",
                ti,
                "--tile-idx",
                str(i),
                "--out-dir",
                tiles,
            ]
        )
    dist_out = os.path.join(md, "registered_dist.ome.tiff")
    run(
        [
            sys.executable,
            "bin/reg_finalize.py",
            "--inputs-dir",
            ti,
            "--tiles-dir",
            tiles,
            "--warp-state",
            os.path.join(md, "warp_state.json"),
            "--src-slide",
            os.path.join(INP, "P001_mov1.ome.tiff"),
            "--out",
            dist_out,
        ]
    )
    # open_multiband, NOT new_from_file: the written slide stores its C channels as C stacked
    # PAGES, so a default read returns channel 0 alone and shape-mismatches base_px below.
    dist_px = warp_tools.vips2numpy(open_multiband(dist_out)).astype(np.float64)

    # --- baseline: VALIS's OWN in-process tiler on the SAME dumped 2-D images, same compose+warp ---
    moving = pyvips.Image.new_from_file(os.path.join(ti, "moving.v"))
    fixed = pyvips.Image.new_from_file(os.path.join(ti, "fixed.v"))
    mask = (
        pyvips.Image.new_from_file(os.path.join(ti, "mask.v"))
        if manifest["has_mask"]
        else None
    )
    reg = NonRigidTileRegistrar(tile_wh=TILE_WH, tile_buffer=TILE_BUFFER)
    _, _, in_proc_bk = reg.register(
        moving,
        fixed,
        mask=mask,
        non_rigid_registrar_cls=OpticalFlowWarper,
        processing_cls=None,
        processing_kwargs=None,
        target_stats=None,
    )

    ws = json.load(open(os.path.join(md, "warp_state.json")))
    reg_mask_f = os.path.join(ti, "reg_mask.npy")
    reg_mask = np.load(reg_mask_f) if os.path.exists(reg_mask_f) else None
    moving_bk = (
        in_proc_bk
        if isinstance(in_proc_bk, pyvips.Image)
        else warp_tools.numpy2vips(
            np.dstack([np.asarray(in_proc_bk[0]), np.asarray(in_proc_bk[1])])
        ).cast("float")
    )
    incoming = pyvips.Image.black(moving_bk.width, moving_bk.height, bands=2).cast(
        "float"
    )
    self_bk = reg_finalize.compose(
        moving_bk, incoming, reg_mask, from_rigid_reg=ws.get("from_rigid_reg", False)
    )
    ipad = ws["internal_pad"]
    os_rc, pb = ipad["out_shape"], ipad["bbox"]
    slide_bk = (
        pyvips.Image.black(os_rc[1], os_rc[0], bands=2)
        .cast("float")
        .insert(reg_finalize._to_vips_field(self_bk), pb[0], pb[1])
    )
    base_warped = slide_tools.warp_slide(
        os.path.join(INP, "P001_mov1.ome.tiff"),
        transformation_src_shape_rc=tuple(ws["processed_img_shape_rc"]),
        transformation_dst_shape_rc=tuple(ws["reg_img_shape_rc"]),
        aligned_slide_shape_rc=tuple(ws["aligned_slide_shape_rc"]),
        M=np.asarray(ws["M"]),
        dxdy=slide_bk,
        level=0,
        series=ws.get("series"),
        interp_method=ws.get("interp_method", "bicubic"),
        bbox_xywh=tuple(ws["bbox_xywh"]) if ws.get("bbox_xywh") else None,
        bg_color=ws.get("bg_color"),
    )
    base_px = warp_tools.vips2numpy(base_warped).astype(np.float64)

    # --- compare ---
    if dist_px.shape != base_px.shape:
        print(
            f"[verify] SHAPE MISMATCH dist={dist_px.shape} base={base_px.shape}",
            flush=True,
        )
        return 1
    equal = np.array_equal(dist_px, base_px)
    d = float(np.max(np.abs(dist_px - base_px)))
    print("\n" + "=" * 72)
    print(
        f"distributed vs in-process tiler (same 2-D inputs): equal={equal} max|Δ|={d}"
    )
    print(f"DISTRIBUTED TILED REGISTRATION BIT-IDENTICAL: {equal}")
    print("=" * 72, flush=True)
    return 0 if equal else 1


if __name__ == "__main__":
    raise SystemExit(main())
