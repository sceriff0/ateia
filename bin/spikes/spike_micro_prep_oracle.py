#!/usr/bin/env python3
"""DIAGNOSTIC (Task 2 micro wiring) — capture the exact field forms classic register()+register_micro()
uses, so REG_MICRO_PREP / REG_FINALIZE can reproduce them by construction.

We must answer, with concrete shapes from a real run:
  Q1. What is slide.bk_dxdy (and fwd) AFTER register() (pre-micro)? shape + a content hash.
      (this is the field REG_MICRO_PREP must INJECT before register_micro's updating prep)
  Q2. What 2-D moving/fixed does OpticalFlowWarper.register receive DURING register_micro?
      (REG_MICRO_PREP must capture the SAME inputs -> REG_NONRIGID computes the residual on them)
  Q3. What is nr_obj.bk_dxdy (the COMPOSED micro residual, post calc_deformation) + its shape vs
      micro full_out_shape_rc + the pad_displacement(out_shape, bbox) call args?
  Q4. What is slide.bk_dxdy AFTER register_micro (the updated field used for the final warp)? shape.
  Q5. Do processed_img_shape_rc / reg_img_shape_rc change between register() and register_micro()?

Run:
  docker run --rm -v "$PWD":/work -w /work mirage-valis:1.0.0 bash -lc '
    python3 tests/testdata/generate_large_fixture.py --size 1024 --out /tmp/bigdata &&
    python3 bin/spikes/spike_micro_prep_oracle.py'
"""
import hashlib
import os
import shutil

import numpy as np
import pyvips

from valis import registration, warp_tools
from valis import serial_non_rigid as snr
from valis.non_rigid_registrars import OpticalFlowWarper

WORK = "/tmp/micro_oracle"
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


def h(a):
    return hashlib.md5(np.ascontiguousarray(to_np(a)).tobytes()).hexdigest()[:12]


def shp(f):
    a = to_np(f)
    return a.shape


def main():
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(INP)
    for s in (REF, MOV):
        shutil.copy(os.path.join(DATADIR, s), os.path.join(INP, s))

    registration.TILER_THRESH_GB = 10  # whole-image (no Blocker 3)
    reg = registration.Valis(INP, os.path.join(WORK, "out"), reference_img_f=REF,
                             align_to_reference=True, crop="reference",
                             non_rigid_registrar_cls=OpticalFlowWarper, create_masks=True,
                             micro_rigid_registrar_cls=None,
                             max_processed_image_dim_px=MAX_PROC, max_non_rigid_registration_dim_px=MAX_NR)
    reg.register()
    s = reg.slide_dict[MOV_STEM]

    print("\n###### Q1: post-register() (pre-micro) #####")
    print(f"  slide.bk_dxdy  shape={shp(s.bk_dxdy)}  hash={h(s.bk_dxdy)}")
    print(f"  slide.fwd_dxdy shape={shp(s.fwd_dxdy)} hash={h(s.fwd_dxdy)}")
    print(f"  processed_img_shape_rc={tuple(s.processed_img_shape_rc)} reg_img_shape_rc={tuple(s.reg_img_shape_rc)}")
    print(f"  aligned_slide_shape_rc={tuple(s.aligned_slide_shape_rc)}")
    print(f"  _full_displacement_shape_rc={tuple(reg._full_displacement_shape_rc)} _non_rigid_bbox={list(reg._non_rigid_bbox)}")
    wave1_bk = to_np(s.bk_dxdy)
    np.save(os.path.join(WORK, "wave1_bk.npy"), wave1_bk)
    np.save(os.path.join(WORK, "wave1_fwd.npy"), to_np(s.fwd_dxdy))

    # ---- hooks to capture micro internals ----
    cap = {}
    orig_calc = snr.NonRigidZImage.calc_deformation
    orig_reg = OpticalFlowWarper.register
    orig_pad = registration.Valis.pad_displacement

    def cap_calc(self, *a, **k):
        out = orig_calc(self, *a, **k)
        if self.name == MOV_STEM:
            cap["residual_bk"] = to_np(self.bk_dxdy)          # COMPOSED micro residual (post calc)
            cap["residual_bk_hash"] = h(self.bk_dxdy)
        return out

    def cap_reg(self, moving_img, fixed_img, mask=None, **kw):
        # capture the micro 2-D inputs, then run the REAL micro deepflow (we want the true residual)
        if cap.get("_grab"):
            cap["micro_moving"] = to_np(moving_img)
            cap["micro_fixed"] = to_np(fixed_img)
            cap["micro_mask"] = None if mask is None else to_np(mask)
            cap["_grab"] = False
        return orig_reg(self, moving_img, fixed_img, mask=mask, **kw)

    def cap_pad(self, dxdy, out_shape_rc, bbox_xywh):
        cap.setdefault("pad_calls", []).append(([int(x) for x in out_shape_rc], [int(x) for x in bbox_xywh]))
        return orig_pad(self, dxdy, out_shape_rc, bbox_xywh)

    cap["_grab"] = True
    snr.NonRigidZImage.calc_deformation = cap_calc
    OpticalFlowWarper.register = cap_reg
    registration.Valis.pad_displacement = cap_pad
    try:
        img_dims = np.array([sl.slide_dimensions_wh[0] for sl in reg.slide_dict.values()])
        micro_size = int(np.floor(np.min([np.max(d) for d in img_dims]) * MICRO_FRACTION))
        reg.register_micro(max_non_rigid_registration_dim_px=micro_size, reference_img_f=REF,
                           align_to_reference=True, tile_wh=2048)
    finally:
        snr.NonRigidZImage.calc_deformation = orig_calc
        OpticalFlowWarper.register = orig_reg
        registration.Valis.pad_displacement = orig_pad

    s2 = reg.slide_dict[MOV_STEM]
    print(f"\n###### Q2: micro 2-D inputs (size param={micro_size}) #####")
    print(f"  micro_moving shape={cap['micro_moving'].shape} hash={h(cap['micro_moving'])}")
    print(f"  micro_fixed  shape={cap['micro_fixed'].shape} hash={h(cap['micro_fixed'])}")
    print(f"  micro_mask   {'None' if cap['micro_mask'] is None else cap['micro_mask'].shape}")
    np.save(os.path.join(WORK, "micro_moving.npy"), cap["micro_moving"])
    np.save(os.path.join(WORK, "micro_fixed.npy"), cap["micro_fixed"])

    print("\n###### Q3: micro residual (COMPOSED, post calc_deformation) #####")
    print(f"  residual_bk shape={cap['residual_bk'].shape} hash={cap['residual_bk_hash']}")
    print(f"  pad_calls (out_shape, bbox)={cap.get('pad_calls')}")
    np.save(os.path.join(WORK, "residual_bk.npy"), cap["residual_bk"])

    print("\n###### Q4: post-register_micro() updated field (final warp input) #####")
    print(f"  slide.bk_dxdy  shape={shp(s2.bk_dxdy)} hash={h(s2.bk_dxdy)}")
    print(f"  slide.fwd_dxdy shape={shp(s2.fwd_dxdy)} hash={h(s2.fwd_dxdy)}")
    np.save(os.path.join(WORK, "updated_bk.npy"), to_np(s2.bk_dxdy))

    print("\n###### Q5: shapes after micro (vs Q1) #####")
    print(f"  processed_img_shape_rc={tuple(s2.processed_img_shape_rc)} reg_img_shape_rc={tuple(s2.reg_img_shape_rc)}")
    print(f"  aligned_slide_shape_rc={tuple(s2.aligned_slide_shape_rc)}")
    print(f"  micro full_out (=updated shape)={shp(s2.bk_dxdy)}")
    print("\n[oracle] saved arrays to", WORK, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
