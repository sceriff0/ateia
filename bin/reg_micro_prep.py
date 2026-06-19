#!/usr/bin/env python3
"""REG_MICRO_PREP (spec §5A Option-2): distributed MICRO-registration wave, stage 1.

Micro-registration is VALIS's SECOND non-rigid pass, at higher resolution (micro_reg_size =
floor(min_max_full_res_dim * micro_reg_fraction)). It is the *heavier* pass, so — like wave 1 —
we run its expensive DeepFlow in a separate JVM-free process (REG_NONRIGID), NOT in REG_FINALIZE.

This stage mirrors reg_prep.py (rigid + processed-2-D capture, no-op warper => no DeepFlow, low RAM),
with two micro-specific additions (registration.py:4170-4339):
  1. INJECT the wave-1 backward field onto each moving slide's ``bk_dxdy`` BEFORE the updating-mode
     prep, so ``prep_images_for_large_non_rigid_registration(updating_non_rigid=True)`` warps the
     moving image through ``M`` + wave-1 field (registration.py:3491-3492) before capturing the micro
     2-D inputs. The wave-1 field handed in (``--wave1-field``) is the COMPOSED full field that classic
     ``register()`` leaves on ``slide.bk_dxdy`` (== reg_finalize's padded ``slide_bk``); we synthesize a
     zero ``fwd_dxdy`` of the same shape so register_micro's additive compose (reads ``fwd_dxdy``
     @registration.py:4300) doesn't crash — its output is discarded; FINALIZE recomputes the warp field.
  2. Capture, per moving slide, the micro 2-D inputs (the ``valis_tiling`` contract REG_NONRIGID/REG_TILE
     read) + a ``micro_warp_state.json`` carrying ``full_out_shape_rc`` and ``mask_bbox`` (for the
     additive pad at registration.py:4314) and ``from_rigid_reg`` (for the §6.3 micro compose).

The additive compose (scale(wave1) + pad(residual)) is proven max|Δ|=0 by spike_micro_option2.py.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "utils"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # for reg_finalize

# Read-only $HOME on HPC clusters breaks numba's on-disk cache during the valis
# import (RuntimeError: '_repeat_1d': no locator available). Redirect + disable
# before importing valis (mirrors register.py).
os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/numba_cache")
os.environ["NUMBA_DISABLE_CACHING"] = "1"
import hpc_scratch  # noqa: F401  redirect cjdk/matplotlib caches off read-only $HOME

import numpy as np
import pyvips
import valis_tiling
import reg_finalize  # reuse the EXACT wave-1 compose+pad that produces classic slide.bk_dxdy
from micro_rigid_guard import install_micro_rigid_guard
from valis_config import build_registrar_kwargs, micro_reg_size as compute_micro_reg_size
from valis import registration
from valis import serial_non_rigid as snr
from valis.non_rigid_registrars import OpticalFlowWarper, NonRigidTileRegistrar


def _jd(o):
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(type(o))


def _vips_to_numpy_pair(vimg):
    """A 2-band vips field -> VALIS's numpy [dx, dy] representation (what slide.bk_dxdy expects).

    Cast to **float64**: classic's non-tiler ``register()`` leaves ``slide.bk_dxdy`` as numpy float64,
    and ``register_micro``'s updating-prep warp is dtype-sensitive. Injecting the vips field's native
    float32 instead caused a sub-quantization warp difference — invisible at low resolution but ≤2
    gray-levels at high resolution (amplifying to ~80 in the registered output at sharp edges). Matching
    float64 makes the distributed micro chain bit-identical to classic at BOTH memory modes (max|Δ|=0)."""
    from valis import warp_tools
    arr = warp_tools.vips2numpy(vimg)  # (H, W, 2)
    return np.array([arr[..., 0], arr[..., 1]]).astype(np.float64)


def _composed_wave1_field(slide_name, wave1_dir, prep_dir):
    """Reconstruct classic ``slide.bk_dxdy`` after register() (the (full_disp) composed wave-1 field)
    from REG_NONRIGID's raw field + REG_PREP's wave-1 warp_state + reg_mask (== reg_finalize's slide_bk).
    Returned as a numpy [dx, dy] pair — the representation classic's non-tiler path leaves on
    ``slide.bk_dxdy`` (injecting vips instead diverges badly at high res: max|Δ|≈229)."""
    raw_bk = pyvips.Image.new_from_file(os.path.join(wave1_dir, slide_name, "bk.v"))
    ws = json.load(open(os.path.join(prep_dir, slide_name, "warp_state.json")))
    rm_f = os.path.join(prep_dir, slide_name, "tiler_inputs", "reg_mask.npy")
    reg_mask = np.load(rm_f) if os.path.exists(rm_f) else None
    slide_bk = reg_finalize.compose_and_pad(raw_bk, ws, reg_mask)   # vips, (full_disp)
    return _vips_to_numpy_pair(slide_bk)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--reference", required=True, help="reference image basename")
    ap.add_argument("--prep-dir", required=True,
                    help="REG_PREP output (per-patient): <slide>/warp_state.json + tiler_inputs/reg_mask.npy")
    ap.add_argument("--wave1-dir", required=True,
                    help="per-slide RAW wave-1 fields from REG_NONRIGID: <slide>/bk.v "
                         "(composed+padded here to reproduce classic slide.bk_dxdy before injection)")
    ap.add_argument("--tile-wh", type=int, default=2048, help="micro tile size (classic uses 2048)")
    ap.add_argument("--tile-buffer", type=int, default=100)
    ap.add_argument("--memory-mode", default="high", choices=["high", "medium", "low"])
    ap.add_argument("--skip-micro-registration", action="store_true",
                    help="MUST match REG_PREP: reg_micro_prep re-runs rigid from scratch, so the rigid "
                         "config (incl. MicroRigidRegistrar) must be identical to REG_PREP for the same M.")
    ap.add_argument("--micro-fraction", type=float, default=0.125)
    ap.add_argument("--max-image-dim", type=int, default=4000)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    install_micro_rigid_guard()  # robust micro-rigid (no-op on real data; see module)
    # Force the processed-2-D branch (no internal tiler), then no-op the warper => no DeepFlow.
    registration.TILER_THRESH_GB = 10 ** 9
    cap = {}     # slide_name -> {moving, fixed, mask, from_rigid, reg_mask, full_out, mask_bbox}
    cur = {}

    orig_calc = snr.NonRigidZImage.calc_deformation

    def cap_calc(self, registered_fixed_image, non_rigid_reg_class, bk_dxdy=None, params=None, mask=None):
        cur["name"] = self.name
        cap.setdefault(self.name, {})
        cap[self.name].update({"from_rigid": bool(self.reg_obj.from_rigid_reg),
                               "reg_mask": None if mask is None else np.asarray(mask)})
        return orig_calc(self, registered_fixed_image, non_rigid_reg_class,
                         bk_dxdy=bk_dxdy, params=params, mask=mask)

    orig_reg = OpticalFlowWarper.register

    def noop_register(self, moving_img, fixed_img, mask=None, **kw):
        nm = cur.get("name")
        cap.setdefault(nm, {})
        cap[nm].update({"moving": moving_img, "fixed": fixed_img, "mask": mask})
        if isinstance(moving_img, pyvips.Image):
            h, w = moving_img.height, moving_img.width
        else:
            h, w = moving_img.shape[0:2]
        z = [np.zeros((h, w)), np.zeros((h, w))]
        return moving_img, z, z

    # IDENTICAL rigid config to REG_PREP (re-running rigid from scratch must yield the SAME M).
    kwargs = build_registrar_kwargs(reference_img_f=args.reference, memory_mode=args.memory_mode,
                                    skip_micro_registration=args.skip_micro_registration,
                                    max_image_dim_px=args.max_image_dim)

    ref_stem = (args.reference.replace(".ome.tiff", "").replace(".ome.tif", "")
                .replace(".tiff", "").replace(".tif", ""))

    # Stage 1: rebuild rigid + wave-1 prep state with the no-op warper (cheap), like reg_prep.
    snr.NonRigidZImage.calc_deformation = cap_calc
    OpticalFlowWarper.register = noop_register
    try:
        reg = registration.Valis(args.input_dir, args.out, **kwargs)
        reg.register()

        # INJECT wave-1 composed field onto each moving slide (so updating prep warps through it).
        for name, slide_obj in reg.slide_dict.items():
            if name == ref_stem:
                continue
            if not os.path.exists(os.path.join(args.wave1_dir, name, "bk.v")):
                continue
            slide_obj.bk_dxdy = _composed_wave1_field(name, args.wave1_dir, args.prep_dir)  # numpy [dx,dy]
            slide_obj.fwd_dxdy = [np.zeros_like(slide_obj.bk_dxdy[0]),
                                  np.zeros_like(slide_obj.bk_dxdy[1])]  # placeholder; discarded by FINALIZE
            slide_obj.stored_dxdy = False

        # Reset capture for the micro pass (we want the micro 2-D inputs, not wave-1's).
        for k in list(cap.keys()):
            for sub in ("moving", "fixed", "mask"):
                cap[k].pop(sub, None)

        micro_size = compute_micro_reg_size(reg.slide_dict, micro_reg_fraction=args.micro_fraction)
        print(f"[reg_micro_prep] micro_reg_size={micro_size}px (fraction={args.micro_fraction})", flush=True)
        reg.register_micro(max_non_rigid_registration_dim_px=micro_size,
                           reference_img_f=args.reference, align_to_reference=True,
                           tile_wh=args.tile_wh)
    finally:
        snr.NonRigidZImage.calc_deformation = orig_calc
        OpticalFlowWarper.register = orig_reg

    # Micro full_out_shape / mask_bbox are per-Valis, set unconditionally at register_micro's end
    # (registration.py:4374-4375) — more reliable than hooking pad_displacement (which only fires when
    # the residual is smaller than full_out). They are the SAME for every slide in the micro pass.
    micro_full_out = [int(x) for x in reg._full_displacement_shape_rc]
    micro_bbox = [int(x) for x in reg._non_rigid_bbox]

    # Dump per moving slide: micro tiler_inputs (2-D images + grid) + micro_warp_state.json
    slides_written = []
    for name, rec in cap.items():
        if name == ref_stem or "moving" not in rec:
            continue
        sdir = os.path.join(args.out, name)
        tiler_inputs = os.path.join(sdir, "tiler_inputs")
        os.makedirs(tiler_inputs, exist_ok=True)

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

        if rec.get("reg_mask") is not None:
            np.save(os.path.join(tiler_inputs, "reg_mask.npy"), rec["reg_mask"])

        mws = {
            "slide_name": name,
            "from_rigid_reg": rec.get("from_rigid", False),
            "micro_full_out_shape_rc": micro_full_out,
            "micro_mask_bbox_xywh": micro_bbox,
        }
        with open(os.path.join(sdir, "micro_warp_state.json"), "w") as fh:
            json.dump(mws, fh, indent=2, default=_jd)
        slides_written.append(name)
        print(f"[reg_micro_prep] dumped {name}: micro tiler_inputs + micro_warp_state "
              f"(full_out={mws['micro_full_out_shape_rc']} bbox={mws['micro_mask_bbox_xywh']})", flush=True)

    registration.kill_jvm()
    print(f"[reg_micro_prep] DONE — {len(slides_written)} moving slide(s): {slides_written}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
