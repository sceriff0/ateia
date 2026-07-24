#!/usr/bin/env python3
"""Classic VALIS vs distributed-tiled — final-result comparison across registration depths.

Runs inside bolt3x/attend_image_analysis:mirage_valis_1.0.0 on the P001 fluorescence pair. Produces the warped MOVING slide
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
  docker run --rm -v "$PWD":/work -w /work bolt3x/attend_image_analysis:mirage_valis_1.0.0 \
      python3 tests/integration/compare_classic_vs_distributed.py
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
import reg_finalize  # also puts bin/utils on sys.path
from mirage_slide_reader import open_multiband  # noqa: E402
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
    print(
        f"  {name:34s} : equal={eq!s:5s} max|Δ|={d.max():.4g} mean|Δ|={d.mean():.4g} "
        f"pixels_diff={npix}/{d.size} ({100.0 * npix / d.size:.2f}%)"
    )


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
    registration.TILER_THRESH_GB = (
        10  # do not tile -> classic whole-image (works on fluorescence)
    )
    # Use the SAME shared config builder classic register.py + distributed reg_prep.py use, so this
    # tests bit-identicality against the user's REAL config (memory_mode + micro). CMP_MEMORY_MODE
    # selects the preset; CMP_SKIP_MICRO toggles MicroRigidRegistrar in the rigid stage.
    sys.path.insert(
        0,
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin", "utils"
        ),
    )
    from valis_config import build_registrar_kwargs

    MEMORY_MODE = os.environ.get("CMP_MEMORY_MODE", "high")
    SKIP_MICRO = os.environ.get("CMP_SKIP_MICRO", "0") == "1"
    kwargs = build_registrar_kwargs(
        reference_img_f=REF, memory_mode=MEMORY_MODE, skip_micro_registration=SKIP_MICRO
    )
    reg = registration.Valis(INP, os.path.join(WORK, "classic_out"), **kwargs)
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
        reg.register_micro(
            max_non_rigid_registration_dim_px=micro_size,
            reference_img_f=REF,
            align_to_reference=True,
            tile_wh=2048,
        )
        classic_C = npx(
            reg.slide_dict[MOV_STEM].warp_slide(level=0, non_rigid=True, crop=True)
        )
    except Exception as e:
        print(
            f"[classic] micro failed (size={micro_size}): {type(e).__name__}: {e}",
            flush=True,
        )

    # ---------------- DISTRIBUTED ----------------
    prep_cmd = [
        sys.executable,
        "bin/reg_prep.py",
        "--input-dir",
        INP,
        "--out",
        PREP,
        "--reference",
        REF,
        "--tile-wh",
        str(TILE_WH),
        "--tile-buffer",
        str(TILE_BUFFER),
        "--memory-mode",
        MEMORY_MODE,
    ]
    if SKIP_MICRO:
        prep_cmd.append("--skip-micro-registration")
    run(prep_cmd)
    md = os.path.join(PREP, MOV_STEM)
    ti, tiles = os.path.join(md, "tiler_inputs"), os.path.join(md, "tiles")
    os.makedirs(tiles, exist_ok=True)
    man = json.load(open(os.path.join(ti, "manifest.json")))
    for i in range(man["n_tiles"]):
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
    src = os.path.join(INP, MOV)
    # A) distributed rigid-only
    run(
        [
            sys.executable,
            "bin/reg_finalize.py",
            "--rigid-only",
            "--warp-state",
            os.path.join(md, "warp_state.json"),
            "--src-slide",
            src,
            "--out",
            os.path.join(md, "dist_A.ome.tiff"),
        ]
    )
    dist_A = npx(open_multiband(os.path.join(md, "dist_A.ome.tiff")))
    # B) distributed rigid + non-rigid (tiled)
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
            src,
            "--out",
            os.path.join(md, "dist_B.ome.tiff"),
        ]
    )
    dist_B = npx(open_multiband(os.path.join(md, "dist_B.ome.tiff")))

    # B-sep) distributed rigid + non-rigid SEPARATED (whole-image, JVM-free) -- the user's primary path.
    # Expected EXACTLY == classic whole-image (same OpticalFlowWarper on the same 2-D inputs).
    run(
        [
            sys.executable,
            "bin/reg_nonrigid.py",
            "--inputs-dir",
            ti,
            "--out-dir",
            os.path.join(md, "nr"),
        ]
    )
    run(
        [
            sys.executable,
            "bin/reg_finalize.py",
            "--inputs-dir",
            ti,
            "--field",
            os.path.join(md, "nr", "bk.v"),
            "--warp-state",
            os.path.join(md, "warp_state.json"),
            "--src-slide",
            src,
            "--out",
            os.path.join(md, "dist_Bsep.ome.tiff"),
        ]
    )
    dist_Bsep = npx(open_multiband(os.path.join(md, "dist_Bsep.ome.tiff")))

    # in-process tiler reference (faithfulness baseline for B)
    moving = pyvips.Image.new_from_file(os.path.join(ti, "moving.v"))
    fixed = pyvips.Image.new_from_file(os.path.join(ti, "fixed.v"))
    mask = (
        pyvips.Image.new_from_file(os.path.join(ti, "mask.v"))
        if man["has_mask"]
        else None
    )
    t = NonRigidTileRegistrar(tile_wh=TILE_WH, tile_buffer=TILE_BUFFER)
    _, _, ip_bk = t.register(
        moving,
        fixed,
        mask=mask,
        non_rigid_registrar_cls=OpticalFlowWarper,
        processing_cls=None,
        processing_kwargs=None,
        target_stats=None,
    )
    ws = json.load(open(os.path.join(md, "warp_state.json")))
    ipad = ws["internal_pad"]
    self_bk = reg_finalize.compose(
        ip_bk,
        pyvips.Image.black(
            ip_bk.width if isinstance(ip_bk, pyvips.Image) else ip_bk[0].shape[1],
            ip_bk.height if isinstance(ip_bk, pyvips.Image) else ip_bk[0].shape[0],
            bands=2,
        ).cast("float"),
        np.load(os.path.join(ti, "reg_mask.npy"))
        if os.path.exists(os.path.join(ti, "reg_mask.npy"))
        else None,
        from_rigid_reg=ws.get("from_rigid_reg", False),
    )
    slide_bk = (
        pyvips.Image.black(ipad["out_shape"][1], ipad["out_shape"][0], bands=2)
        .cast("float")
        .insert(reg_finalize._to_vips_field(self_bk), ipad["bbox"][0], ipad["bbox"][1])
    )
    inproc_B = npx(
        slide_tools.warp_slide(
            src,
            tuple(ws["processed_img_shape_rc"]),
            tuple(ws["reg_img_shape_rc"]),
            tuple(ws["aligned_slide_shape_rc"]),
            M=np.asarray(ws["M"]),
            dxdy=slide_bk,
            level=0,
            series=ws.get("series"),
            bbox_xywh=tuple(ws["bbox_xywh"]),
            bg_color=ws.get("bg_color"),
        )
    )

    # ---------------- REPORT ----------------
    print("\n" + "=" * 78)
    print(
        "CLASSIC (whole-image) vs DISTRIBUTED (tiled) — warped MOVING slide, fluorescence"
    )
    print("=" * 78)
    report("A  rigid-only:  classic vs dist", classic_A, dist_A)
    report("B  +non-rigid:  classic vs dist-SEPARATED", classic_B, dist_Bsep)
    report("B  +non-rigid:  classic vs dist-tiled", classic_B, dist_B)
    report("B  +non-rigid:  dist-tiled vs in-proc tiler", dist_B, inproc_B)
    report("C  +micro:      classic (shown)", classic_C, classic_C)
    print("   C  distributed micro: verified separately (verify_micro_bitidentical.py)")
    print("=" * 78, flush=True)

    # ---------------- PARITY GATE (the "normal == distributed" guarantee) ----------------
    # The DEFAULT distributed path (reg_dist_force_tiling=false) is SEPARATED whole-image non-rigid:
    # the SAME OpticalFlowWarper on the SAME 2-D inputs classic uses, just in a JVM-free process. It
    # must therefore be BIT-IDENTICAL to classic (spec §6.7). This is the guarantee users rely on when
    # they flip reg_distributed_tiling on. Fail loudly if it ever drifts (e.g. a float-precision
    # regression in the bk.v handoff — see reg_nonrigid._save_field contract).
    atol = float(
        os.environ.get("CMP_SEPARATED_ATOL", "0")
    )  # 0 == exact (design claim: max|Δ|=0)
    ok = True
    if (
        classic_A is None
        or dist_A is None
        or float(np.max(np.abs(classic_A - dist_A))) > atol
    ):
        print(
            "PARITY FAIL: rigid-only classic != distributed (should be exact)",
            flush=True,
        )
        ok = False
    if classic_B is None or dist_Bsep is None or classic_B.shape != dist_Bsep.shape:
        print(
            "PARITY FAIL: SEPARATED non-rigid missing / shape mismatch vs classic",
            flush=True,
        )
        ok = False
    elif float(np.max(np.abs(classic_B - dist_Bsep))) > atol:
        print(
            f"PARITY FAIL: SEPARATED non-rigid max|Δ|={float(np.max(np.abs(classic_B - dist_Bsep))):.4g} "
            f"> atol={atol} — distributed default path is NOT bit-identical to classic.",
            flush=True,
        )
        ok = False
    print(
        f"PARITY (classic == distributed SEPARATED default path): {'PASS' if ok else 'FAIL'}",
        flush=True,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
