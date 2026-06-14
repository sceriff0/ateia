#!/usr/bin/env python3
"""Classic VALIS vs distributed-tiled — final-result comparison across registration depths.

Runs inside mirage-valis:1.0.0 on the P001 fluorescence pair. Produces the warped MOVING slide
three ways for classic and (where implemented) distributed, and reports pixel deltas:

  A) rigid only                 (warp with M + crop, no non-rigid)
  B) rigid + non-rigid          (1st DeepFlow pass)
  C) rigid + non-rigid + micro  (2nd DeepFlow pass, register_micro tile_wh=2048)

Key expectations (see spec §6.5/§6.6):
  * A: classic == distributed  EXACTLY (same rigid M + crop; no algorithm difference).
  * B: classic (whole-image DeepFlow) != distributed (tiled DeepFlow) — different algorithms below
       the 10GB tiler threshold (~8px, §6.5). distributed == in-process TILER exactly (the faithful
       baseline). The "classic vs distributed" delta here is the inherent whole-vs-tiled gap, which
       the below-threshold->classic fallback removes for small images.
  * C: classic micro shown; distributed micro is pending Option-2 (2nd wave) implementation.

Usage:
  docker run --rm -v "$PWD":/work -w /work mirage-valis:1.0.0 \
      python3 tests/integration/compare_classic_vs_distributed.py
"""
import json
import os
import shutil
import subprocess
import sys

import numpy as np
import pyvips

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin"))
import reg_finalize
from valis import registration, slide_tools, warp_tools
from valis.non_rigid_registrars import NonRigidTileRegistrar, OpticalFlowWarper

WORK = "/tmp/cmp"
INP = os.path.join(WORK, "in")
PREP = os.path.join(WORK, "prep")
REF, MOV = "P001_ref.ome.tiff", "P001_mov1.ome.tiff"
MOV_STEM = "P001_mov1"
# Defaults suit the 128px fixture; override via env for a larger fixture (see generate_large_fixture.py).
DATADIR = os.environ.get("CMP_DATADIR", "tests/testdata")
TILE_WH = int(os.environ.get("CMP_TILE_WH", "48"))
TILE_BUFFER = int(os.environ.get("CMP_TILE_BUFFER", "16"))
MAX_NR = int(os.environ.get("CMP_MAX_NR", "1024"))
MAX_PROC = int(os.environ.get("CMP_MAX_PROC", "256"))
MICRO_FRACTION = float(os.environ.get("CMP_MICRO_FRACTION", "0.125"))


def npx(vips_img):
    return warp_tools.vips2numpy(vips_img).astype(np.float64)


def report(name, a, b):
    if a is None or b is None:
        print(f"  {name:34s} : (skipped)")
        return
    if a.shape != b.shape:
        print(f"  {name:34s} : SHAPE {a.shape} vs {b.shape}")
        return
    d = np.abs(a - b)
    eq = bool(np.array_equal(a, b))
    npix = int((d > 0).sum())
    print(f"  {name:34s} : equal={eq!s:5s} max|Δ|={d.max():.4g} mean|Δ|={d.mean():.4g} "
          f"pixels_diff={npix}/{d.size} ({100.0*npix/d.size:.2f}%)")


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write("\n".join((r.stdout + r.stderr).splitlines()[-15:]))
        raise SystemExit(f"FAILED: {' '.join(cmd[1:4])}")


def main():
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(INP)
    for s in (REF, MOV):
        shutil.copy(os.path.join(DATADIR, s), os.path.join(INP, s))

    # ---------------- CLASSIC (whole-image VALIS) ----------------
    registration.TILER_THRESH_GB = 10  # do not tile -> classic whole-image (works on fluorescence)
    reg = registration.Valis(
        INP, os.path.join(WORK, "classic_out"), reference_img_f=REF,
        align_to_reference=True, crop="reference", non_rigid_registrar_cls=OpticalFlowWarper,
        create_masks=True, micro_rigid_registrar_cls=None,
        max_processed_image_dim_px=MAX_PROC, max_non_rigid_registration_dim_px=MAX_NR)
    reg.register()
    mov_slide = reg.slide_dict[MOV_STEM]
    classic_A = npx(mov_slide.warp_slide(level=0, non_rigid=False, crop=True))
    classic_B = npx(mov_slide.warp_slide(level=0, non_rigid=True, crop=True))

    # C: micro (2nd pass). micro_reg_size = floor(min_max_size * fraction)
    img_dims = np.array([s.slide_dimensions_wh[0] for s in reg.slide_dict.values()])
    min_max_size = np.min([np.max(d) for d in img_dims])
    micro_size = int(np.floor(min_max_size * MICRO_FRACTION))
    classic_C = None
    try:
        reg.register_micro(max_non_rigid_registration_dim_px=micro_size, reference_img_f=REF,
                           align_to_reference=True, tile_wh=2048)
        classic_C = npx(reg.slide_dict[MOV_STEM].warp_slide(level=0, non_rigid=True, crop=True))
    except Exception as e:
        print(f"[classic] micro failed (size={micro_size}): {type(e).__name__}: {e}", flush=True)

    # ---------------- DISTRIBUTED ----------------
    run([sys.executable, "bin/reg_prep.py", "--input-dir", INP, "--out", PREP, "--reference", REF,
         "--tile-wh", str(TILE_WH), "--tile-buffer", str(TILE_BUFFER),
         "--max-non-rigid-dim", str(MAX_NR), "--max-processed-dim", str(MAX_PROC)])
    md = os.path.join(PREP, MOV_STEM)
    ti, tiles = os.path.join(md, "tiler_inputs"), os.path.join(md, "tiles")
    os.makedirs(tiles, exist_ok=True)
    man = json.load(open(os.path.join(ti, "manifest.json")))
    for i in range(man["n_tiles"]):
        run([sys.executable, "bin/reg_tile.py", "--inputs-dir", ti, "--tile-idx", str(i), "--out-dir", tiles])
    src = os.path.join(INP, MOV)
    # A) distributed rigid-only
    run([sys.executable, "bin/reg_finalize.py", "--rigid-only", "--warp-state", os.path.join(md, "warp_state.json"),
         "--src-slide", src, "--out", os.path.join(md, "dist_A.ome.tiff")])
    dist_A = npx(pyvips.Image.new_from_file(os.path.join(md, "dist_A.ome.tiff")))
    # B) distributed rigid + non-rigid (tiled)
    run([sys.executable, "bin/reg_finalize.py", "--inputs-dir", ti, "--tiles-dir", tiles,
         "--warp-state", os.path.join(md, "warp_state.json"), "--src-slide", src,
         "--out", os.path.join(md, "dist_B.ome.tiff")])
    dist_B = npx(pyvips.Image.new_from_file(os.path.join(md, "dist_B.ome.tiff")))

    # in-process tiler reference (faithfulness baseline for B)
    moving = pyvips.Image.new_from_file(os.path.join(ti, "moving.v"))
    fixed = pyvips.Image.new_from_file(os.path.join(ti, "fixed.v"))
    mask = pyvips.Image.new_from_file(os.path.join(ti, "mask.v")) if man["has_mask"] else None
    t = NonRigidTileRegistrar(tile_wh=TILE_WH, tile_buffer=TILE_BUFFER)
    _, _, ip_bk = t.register(moving, fixed, mask=mask, non_rigid_registrar_cls=OpticalFlowWarper,
                             processing_cls=None, processing_kwargs=None, target_stats=None)
    ws = json.load(open(os.path.join(md, "warp_state.json")))
    ipad = ws["internal_pad"]
    self_bk = reg_finalize.compose(ip_bk, pyvips.Image.black(
        ip_bk.width if isinstance(ip_bk, pyvips.Image) else ip_bk[0].shape[1],
        ip_bk.height if isinstance(ip_bk, pyvips.Image) else ip_bk[0].shape[0], bands=2).cast("float"),
        np.load(os.path.join(ti, "reg_mask.npy")) if os.path.exists(os.path.join(ti, "reg_mask.npy")) else None,
        from_rigid_reg=ws.get("from_rigid_reg", False))
    slide_bk = pyvips.Image.black(ipad["out_shape"][1], ipad["out_shape"][0], bands=2).cast("float").insert(
        reg_finalize._to_vips_field(self_bk), ipad["bbox"][0], ipad["bbox"][1])
    inproc_B = npx(slide_tools.warp_slide(src, tuple(ws["processed_img_shape_rc"]), tuple(ws["reg_img_shape_rc"]),
                  tuple(ws["aligned_slide_shape_rc"]), M=np.asarray(ws["M"]), dxdy=slide_bk, level=0,
                  series=ws.get("series"), bbox_xywh=tuple(ws["bbox_xywh"]), bg_color=ws.get("bg_color")))

    # ---------------- REPORT ----------------
    print("\n" + "=" * 78)
    print("CLASSIC (whole-image) vs DISTRIBUTED (tiled) — warped MOVING slide, fluorescence")
    print("=" * 78)
    report("A  rigid-only:  classic vs dist", classic_A, dist_A)
    report("B  +non-rigid:  classic vs dist", classic_B, dist_B)
    report("B  +non-rigid:  dist vs in-proc tiler", dist_B, inproc_B)
    report("C  +micro:      classic (shown)", classic_C, classic_C)
    print("   C  distributed micro: PENDING Option-2 (2nd wave) implementation")
    print("=" * 78, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
