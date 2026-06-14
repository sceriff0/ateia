#!/usr/bin/env python3
"""SPIKE (Task 2 / Option 2) — prove distributed micro-registration's NEW mechanics are reproducible.

Runs in mirage-valis:1.0.0 on a larger fixture (micro is degenerate at 128px). Micro = a 2nd non-rigid
pass via the SAME serial_non_rigid -> NonRigidTileRegistrar path as wave 1 (so REG_TILE is reusable),
but with two NEW pieces vs wave 1 (registration.py:4290-4339):
  1. updating_non_rigid=True prep: the moving image is warped through M + the wave-1 field before the
     micro pass computes the RESIDUAL.
  2. ADDITIVE compose: updated = scale(wave1_field -> micro_out_shape) + pad(residual). Both bk AND
     fwd are updated additively (NOT via get_inverse_field).

This spike captures, from a classic register()+register_micro() run, the wave-1 field, the micro
residual (the tiler's stitched output), the classic UPDATED field, and verifies we can REPRODUCE the
updated field from plain data: scale(wave1) + pad(residual)  == classic updated  (EXACT).
That is the new make-or-break for Option 2; the tile compute itself is already proven (§6.1).

Usage:
  docker run --rm -v "$PWD":/work -w /work mirage-valis:1.0.0 bash -lc '
    python3 tests/testdata/generate_large_fixture.py --size 1024 --out /tmp/bigdata &&
    python3 bin/spikes/spike_micro_option2.py'
"""
import os
import shutil

import numpy as np
import pyvips

from valis import registration, warp_tools
from valis import serial_non_rigid as snr
from valis.non_rigid_registrars import OpticalFlowWarper

WORK = "/tmp/micro_spike"
INP = os.path.join(WORK, "in")
DATADIR = os.environ.get("CMP_DATADIR", "/tmp/bigdata")
REF, MOV, MOV_STEM = "P001_ref.ome.tiff", "P001_mov1.ome.tiff", "P001_mov1"
MAX_NR, MAX_PROC = 1024, 512
MICRO_FRACTION = 0.125


def to_np(f):
    if isinstance(f, pyvips.Image):
        return warp_tools.vips2numpy(f).astype(np.float64)
    a = np.asarray(f)
    if a.ndim == 3 and a.shape[0] == 2:
        a = np.dstack([a[0], a[1]])
    return a.astype(np.float64)


def main():
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(INP)
    for s in (REF, MOV):
        shutil.copy(os.path.join(DATADIR, s), os.path.join(INP, s))

    registration.TILER_THRESH_GB = 10  # whole-image (micro residual via OpticalFlowWarper, no Blocker 3)
    reg = registration.Valis(INP, os.path.join(WORK, "out"), reference_img_f=REF,
                             align_to_reference=True, crop="reference",
                             non_rigid_registrar_cls=OpticalFlowWarper, create_masks=True,
                             micro_rigid_registrar_cls=None,
                             max_processed_image_dim_px=MAX_PROC, max_non_rigid_registration_dim_px=MAX_NR)
    reg.register()

    # wave-1 field (before micro)
    mov_slide = reg.slide_dict[MOV_STEM]
    wave1_bk = to_np(mov_slide.bk_dxdy)
    wave1_fwd = to_np(mov_slide.fwd_dxdy)

    # capture the micro residual (nr_obj field, pre additive-compose) + the micro prep's
    # full_out_shape_rc/mask_bbox (from the pad_displacement call at registration.py:4314).
    captured = {}
    orig_calc = snr.NonRigidZImage.calc_deformation
    orig_pad = registration.Valis.pad_displacement

    def cap_calc(self, *a, **k):
        out = orig_calc(self, *a, **k)
        if self.name == MOV_STEM:
            captured["residual_bk"] = to_np(self.bk_dxdy)
        return out

    def cap_pad(self, dxdy, out_shape_rc, bbox_xywh):
        captured.setdefault("pad_calls", []).append(([int(x) for x in out_shape_rc],
                                                     [int(x) for x in bbox_xywh]))
        return orig_pad(self, dxdy, out_shape_rc, bbox_xywh)
    snr.NonRigidZImage.calc_deformation = cap_calc
    registration.Valis.pad_displacement = cap_pad
    try:
        img_dims = np.array([s.slide_dimensions_wh[0] for s in reg.slide_dict.values()])
        micro_size = int(np.floor(np.min([np.max(d) for d in img_dims]) * MICRO_FRACTION))
        reg.register_micro(max_non_rigid_registration_dim_px=micro_size, reference_img_f=REF,
                           align_to_reference=True, tile_wh=2048)
    finally:
        snr.NonRigidZImage.calc_deformation = orig_calc
        registration.Valis.pad_displacement = orig_pad

    updated_bk = to_np(reg.slide_dict[MOV_STEM].bk_dxdy)   # classic post-micro field (ground truth)
    res_bk = captured["residual_bk"]
    full_out = tuple(updated_bk.shape[:2])                  # micro full_out_shape_rc
    pad_bbox = captured.get("pad_calls", [None])[0]         # (out_shape, bbox) of the residual pad
    print(f"[micro] micro_size={micro_size} wave1_bk={wave1_bk.shape} residual_bk={res_bk.shape} "
          f"updated_bk={updated_bk.shape} pad_call={pad_bbox}", flush=True)

    # ---- REPRODUCE the additive compose from plain data (registration.py:4312-4330) ----
    # 1) pad residual to full_out_shape at mask_bbox (if micro ran on a sub-region)
    vips_res = warp_tools.numpy2vips(res_bk).cast("float")
    if tuple(res_bk.shape[:2]) != full_out:
        bbox = pad_bbox[1] if pad_bbox else [0, 0, res_bk.shape[1], res_bk.shape[0]]
        vips_new_bk = pyvips.Image.black(full_out[1], full_out[0], bands=2).cast("float").insert(vips_res, bbox[0], bbox[1])
    else:
        vips_new_bk = vips_res
    # 2) scale wave-1 field to full_out_shape (registration.py:4318-4323)
    vips_cur_bk = warp_tools.numpy2vips(wave1_bk).cast("float")
    slide_sxy = (np.array(full_out) / np.array([vips_cur_bk.height, vips_cur_bk.width]))[::-1]
    if not np.all(slide_sxy == 1):
        sx, sy = float(slide_sxy[0]), float(slide_sxy[1])
        scaled = (sx * vips_cur_bk[0]).bandjoin(sy * vips_cur_bk[1])
        vips_cur_bk = warp_tools.resize_img(scaled, full_out)
    # 3) additive (registration.py:4330)
    repro_updated_bk = to_np(vips_cur_bk + vips_new_bk)

    eq = repro_updated_bk.shape == updated_bk.shape and np.array_equal(repro_updated_bk, updated_bk)
    d = float(np.max(np.abs(repro_updated_bk - updated_bk))) if repro_updated_bk.shape == updated_bk.shape else float("nan")
    print("\n" + "=" * 72)
    print(f"micro additive-compose reproduced from plain data: equal={eq} max|Δ|={d}")
    print(f"MICRO OPTION-2 COMPOSE MAKE-OR-BREAK: {eq}")
    print("=" * 72, flush=True)
    return 0 if eq else 1


if __name__ == "__main__":
    raise SystemExit(main())
