#!/usr/bin/env python3
"""REG_PREP (fix-A, spec §6.6): distributed-tiling stage 1.

Run VALIS rigid registration + produce base VALIS's *processed 2-D* non-rigid inputs, but halt
BEFORE the whole-image DeepFlow (so no heavy non-rigid runs here — low RAM). Dump, per moving slide:
the 2-D images + tile grid (the ``valis_tiling`` contract REG_TILE reads) and a plain ``warp_state.json``
(the §6.3 contract REG_FINALIZE reads). No registrar pickle (Blocker 1).

Why this is bit-identical by construction (spec §6.5/§6.6): the dumped 2-D images are VALIS's OWN
processed images (captured live, ChannelGetter w/ src_f present), the tile kernel is VALIS's (§6.1),
and compose+warp is VALIS's (§6.3). So distributed == VALIS's tiler run in-process on its own 2-D image.

To get the processed-2-D images without paying for DeepFlow: force the processed branch
(``TILER_THRESH_GB`` high so ``prep_images_for_large_non_rigid_registration`` processes to 2-D), then
replace ``OpticalFlowWarper.register`` with a no-op that captures its 2-D inputs and returns a zero
field. ``Valis.register()`` then completes cheaply; rigid + non-rigid-prep state persists on the Valis.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "utils"))
import numpy as np
import pyvips
import valis_tiling
from valis import registration, slide_tools
from valis import serial_non_rigid as snr
from valis.non_rigid_registrars import OpticalFlowWarper, NonRigidTileRegistrar
from valis.registration import CROP_REF


def _jd(o):
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(type(o))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--reference", required=True, help="reference image basename")
    ap.add_argument("--tile-wh", type=int, default=512)
    ap.add_argument("--tile-buffer", type=int, default=100)
    ap.add_argument("--max-non-rigid-dim", type=int, default=4096)
    ap.add_argument("--max-processed-dim", type=int, default=2048)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    # Force the processed-2-D non-rigid prep branch (no tiler), then no-op the warper => no DeepFlow.
    registration.TILER_THRESH_GB = 10 ** 9
    cap = {}            # slide_name -> {moving, fixed, mask, from_rigid, incoming_is_none, reg_mask}
    cur = {}

    orig_calc = snr.NonRigidZImage.calc_deformation

    def cap_calc(self, registered_fixed_image, non_rigid_reg_class, bk_dxdy=None, params=None, mask=None):
        cur["name"] = self.name
        cap[self.name] = {"from_rigid": bool(self.reg_obj.from_rigid_reg),
                          "incoming_is_none": bk_dxdy is None,
                          "reg_mask": None if mask is None else np.asarray(mask)}
        return orig_calc(self, registered_fixed_image, non_rigid_reg_class,
                         bk_dxdy=bk_dxdy, params=params, mask=mask)

    orig_reg = OpticalFlowWarper.register

    def noop_register(self, moving_img, fixed_img, mask=None, **kw):
        nm = cur.get("name")
        cap[nm].update({"moving": moving_img, "fixed": fixed_img, "mask": mask})
        if isinstance(moving_img, pyvips.Image):
            h, w = moving_img.height, moving_img.width
        else:
            h, w = moving_img.shape[0:2]
        z = [np.zeros((h, w)), np.zeros((h, w))]   # numpy [dx,dy] no-op field (skips DeepFlow)
        return moving_img, z, z

    snr.NonRigidZImage.calc_deformation = cap_calc
    OpticalFlowWarper.register = noop_register
    try:
        reg = registration.Valis(
            args.input_dir, args.out, reference_img_f=args.reference,
            align_to_reference=True, crop="reference",
            non_rigid_registrar_cls=OpticalFlowWarper, create_masks=True,
            micro_rigid_registrar_cls=None,
            max_processed_image_dim_px=args.max_processed_dim,
            max_non_rigid_registration_dim_px=args.max_non_rigid_dim)
        reg.register()
    finally:
        snr.NonRigidZImage.calc_deformation = orig_calc
        OpticalFlowWarper.register = orig_reg

    ref_stem = args.reference.replace(".ome.tiff", "").replace(".tiff", "")
    ref_slide = reg.get_ref_slide()
    full_disp = [int(x) for x in reg._full_displacement_shape_rc]
    non_rigid_bbox = [int(x) for x in reg._non_rigid_bbox]
    # CROP_REF bbox at level 0 (warp_slide lines 882-889)
    scaled_aligned_rc = list(ref_slide.slide_dimensions_wh[0][::-1])

    slides_written = []
    for name, slide_obj in reg.slide_dict.items():
        if name == ref_stem or name not in cap or "moving" not in cap[name]:
            continue  # only moving slides that actually registered
        rec = cap[name]
        sdir = os.path.join(args.out, name)
        tiler_inputs = os.path.join(sdir, "tiler_inputs")
        os.makedirs(tiler_inputs, exist_ok=True)

        # Dump the tiler-input contract (2-D images + grid) by driving the tiler on the captured 2-D
        # images with the halt hook (grid is set up in register() before calc() -> _dump_inputs -> raise).
        valis_tiling.install_halt_hook(tiler_inputs)
        try:
            t = NonRigidTileRegistrar(tile_wh=args.tile_wh, tile_buffer=args.tile_buffer)
            t.register(rec["moving"], rec["fixed"], mask=rec["mask"],
                       non_rigid_registrar_cls=OpticalFlowWarper,
                       processing_cls=None, processing_kwargs=None, target_stats=None)
        except valis_tiling.TilesPending:
            pass
        finally:
            valis_tiling.clear_hook()

        if rec["reg_mask"] is not None:
            np.save(os.path.join(tiler_inputs, "reg_mask.npy"), rec["reg_mask"])

        slide_bbox_xywh, _ = slide_obj.get_crop_xywh(crop=CROP_REF, out_shape_rc=scaled_aligned_rc)
        ws = {
            "slide_name": name,
            "src_f": slide_obj.src_f,
            "from_rigid_reg": rec["from_rigid"],
            "M": np.asarray(slide_obj.M).tolist(),
            "processed_img_shape_rc": [int(x) for x in slide_obj.processed_img_shape_rc],
            "reg_img_shape_rc": [int(x) for x in slide_obj.reg_img_shape_rc],
            "aligned_slide_shape_rc": [int(x) for x in slide_obj.aligned_slide_shape_rc],
            "bbox_xywh": [int(x) for x in slide_bbox_xywh],
            "bg_color": [float(x) for x in slide_obj.bg_color] if slide_obj.bg_color is not None else None,
            "series": slide_obj.series,
            "is_rgb": bool(slide_obj.is_rgb),
            "interp_method": "bicubic",
            "internal_pad": {"out_shape": full_disp, "bbox": non_rigid_bbox},
        }
        with open(os.path.join(sdir, "warp_state.json"), "w") as fh:
            json.dump(ws, fh, indent=2, default=_jd)
        slides_written.append(name)
        print(f"[reg_prep] dumped {name}: tiler_inputs + warp_state "
              f"(reg_shape={ws['reg_img_shape_rc']} bbox={ws['bbox_xywh']})", flush=True)

    registration.kill_jvm()
    print(f"[reg_prep] DONE — {len(slides_written)} moving slide(s): {slides_written}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
