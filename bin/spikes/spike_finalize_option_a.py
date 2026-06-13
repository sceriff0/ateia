#!/usr/bin/env python3
"""THROWAWAY SPIKE (plan Task 4.5) — prove Option-A FINALIZE is pixel-identical to classic VALIS.

Runs inside mirage-valis:1.0.0. The §6.1 spike already proved the *tile* leg (externalized
tiles + warp_tools.stitch_tiles == in-process calc(), exactly). This spike proves the *other*
leg of the make-or-break: given the stitched non-rigid field `moving_bk_dxdy` (what REG_TILE
produces), can FINALIZE reproduce the warped registered slide from PLAIN DUMPED DATA — no pickled
`Valis` registrar (Blocker 1) — pixel-identical to a full classic run?

It decomposes FINALIZE into independently-localizable proofs, each computed in a FRESH process
from plain data only:

  P1  COMPOSE  — reproduce serial_non_rigid.NonRigidZImage.calc_deformation lines 460-503
                 (remove_invasive -> mask -> add ref field -> remove_invasive -> get_inverse_field)
                 on the captured `moving_bk_dxdy`. Assert == the classic `self.bk_dxdy`. EXACT.
  P3  HANDOFF  — the disk-backed dxdy round-trip the tiler path uses: extract_area(non_rigid_bbox)
                 -> lossless tiffsave -> new_from_file -> Valis.pad_displacement. Assert lossless.
  P2  WARP     — slide_tools.warp_slide(...) is a PURE plain-data fn (src_f, M, shapes, dxdy, bbox,
                 bg_color). Assert it reproduces the classic warped slide. EXACT.
  CHAIN        — moving_bk_dxdy -> [P1] -> pad -> [P2] end-to-end == classic warped slide. EXACT.

Why a WHOLE-IMAGE classic baseline (not tiled): the test slides are 3-channel fluorescence, and
classic auto-tiling itself crashes on fluorescence (Blocker 3, ChannelGetter src_f=None). So we
never force the tiler here; `moving_bk_dxdy` stands in for the stitched field, whose identity to
the in-process field is already proven by the §6.1 spike. Config mirrors production
(bin/register.py): align_to_reference=True (so the incoming/reference field is identity for every
moving slide -> the common compose case, spec §6.2 step 3), crop="reference", create_masks=True
(so the mask branches in compose are exercised), OpticalFlowWarper (DeepFlow), no micro.

We start with the 1-ref + 1-mov pair (spec §6.2: "start the spike there, then generalize").

Usage (inside the image):
  docker run --rm -v "$PWD":/work -w /work mirage-valis:1.0.0 \
      python3 bin/spikes/spike_finalize_option_a.py all
"""
import argparse, os, sys, json, glob, shutil, subprocess, traceback
import numpy as np

WORK = "/tmp/finalize_spike"
DUMP = os.path.join(WORK, "dump")
INPUT_DIR = os.path.join(WORK, "input")
SRC_TESTDATA = "tests/testdata"
REF = "P001_ref.ome.tiff"
MOV = "P001_mov1.ome.tiff"
SELF = os.path.abspath(__file__)

# Test preset (bin/register.py "test"): small dims keep the whole-image run fast + below the tiler.
MAX_PROCESSED_DIM = 256
MAX_NON_RIGID_DIM = 1024


# --------------------------------------------------------------------------- field (de)serialize
# Non-rigid fields are sometimes pyvips.Image, sometimes a [dx, dy] numpy list (calc_deformation
# branches on type, line 467 vs 473). Persist natively so the rebuild reconstructs the SAME type
# and the reproduced VALIS calls are bit-faithful, not silently coerced.
def save_field(field, base):
    import pyvips
    if isinstance(field, pyvips.Image):
        field.write_to_file(base + ".v")
        return "vips"
    arr = np.asarray(field)
    np.save(base + ".npy", arr)
    return "numpy"


def load_field(base):
    import pyvips
    if os.path.exists(base + ".v"):
        return pyvips.Image.new_from_file(base + ".v")
    arr = np.load(base + ".npy")
    return arr  # shape (2, H, W) for the [dx, dy] form


def field_to_np(field):
    """Canonical (H, W, 2) float32 for EXACT comparison, regardless of native type."""
    from valis import warp_tools
    import pyvips
    if isinstance(field, pyvips.Image):
        arr = warp_tools.vips2numpy(field)
    elif isinstance(field, (list, tuple)) or (isinstance(field, np.ndarray) and field.ndim == 3 and field.shape[0] == 2):
        arr = np.dstack([np.asarray(field[0]), np.asarray(field[1])])
    else:
        arr = np.asarray(field)
    return np.ascontiguousarray(arr).astype(np.float32)


def exact(a, b):
    A, B = field_to_np(a), field_to_np(b)
    if A.shape != B.shape:
        return False, float("nan"), f"shape {A.shape} vs {B.shape}"
    d = float(np.max(np.abs(A - B))) if A.size else 0.0
    return np.array_equal(A, B), d, ""


# --------------------------------------------------------------------------- compose (FINALIZE math)
# Verbatim port of serial_non_rigid.NonRigidZImage.calc_deformation, lines 460-503 — the post-tiler
# composition FINALIZE must reproduce. This block IS the future reg_finalize.py compose step.
def _mask_img(img, mask):
    import pyvips
    if isinstance(img, pyvips.Image):
        vmask = mask if isinstance(mask, pyvips.Image) else None
        from valis import warp_tools
        if vmask is None:
            vmask = warp_tools.numpy2vips(np.asarray(mask))
        return (vmask == 0).ifthenelse(0, img)
    out = img.copy()
    out[np.asarray(mask) == 0] = 0
    return out


def _mask_dxdy(dxdy, mask):
    import pyvips
    if isinstance(dxdy, pyvips.Image):
        return _mask_img(dxdy, mask)
    return [_mask_img(dxdy[0], mask), _mask_img(dxdy[1], mask)]


def reproduce_compose(moving_bk_dxdy, incoming_bk_dxdy, reg_mask, from_rigid_reg=False,
                      M=None, unwarped_shape=None, og_reg_shape_rc=None):
    """Replicate calc_deformation 460-503. `incoming_bk_dxdy` is the field of the image this slide
    aligned to (the reference's, which is identity when align_to_reference=True). Returns the
    composed self.bk_dxdy.

    NOTE (spike finding §6.2): in EVERY production preset the non-rigid runs on scaled/masked
    images (max_processed_dim != max_non_rigid_dim, and/or create_masks crops), so VALIS sets
    `from_rigid_reg=False` (serial_non_rigid.py:642-647) and the remove_invasive_displacements
    steps (460-465, 482-487) are SKIPPED. They are gated here for completeness only."""
    import pyvips
    from valis import warp_tools

    if from_rigid_reg:  # 460-465 (NOT taken in production)
        moving_bk_dxdy = warp_tools.remove_invasive_displacements(
            moving_bk_dxdy, M=M, src_shape_rc=unwarped_shape, out_shape_rc=og_reg_shape_rc)

    is_vips = isinstance(moving_bk_dxdy, pyvips.Image)
    if not is_vips:  # 467-472: numpy branch
        if reg_mask is not None:
            moving_bk_dxdy = _mask_dxdy(moving_bk_dxdy, reg_mask)
        bk_dxdy_from_ref = np.array([incoming_bk_dxdy[0] + moving_bk_dxdy[0],
                                     incoming_bk_dxdy[1] + moving_bk_dxdy[1]])
    else:            # 473-476: pyvips branch
        if reg_mask is not None:
            moving_bk_dxdy = _mask_dxdy(moving_bk_dxdy, reg_mask)
        bk_dxdy_from_ref = incoming_bk_dxdy + moving_bk_dxdy

    img_bk_dxdy = bk_dxdy_from_ref.copy() if hasattr(bk_dxdy_from_ref, "copy") else bk_dxdy_from_ref
    if reg_mask is not None:                                       # 479-480
        img_bk_dxdy = _mask_dxdy(img_bk_dxdy, reg_mask)
    if from_rigid_reg:  # 482-487 (NOT taken in production)
        img_bk_dxdy = warp_tools.remove_invasive_displacements(
            img_bk_dxdy, M=M, src_shape_rc=unwarped_shape, out_shape_rc=og_reg_shape_rc)
    return img_bk_dxdy


# --------------------------------------------------------------------------- BASELINE (classic, JVM)
def mode_baseline():
    from valis import registration, slide_tools
    from valis import serial_non_rigid as snr
    from valis.non_rigid_registrars import OpticalFlowWarper

    registration.TILER_THRESH_GB = 10  # DO NOT force the tiler (Blocker 3); whole-image completes.
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(INPUT_DIR); os.makedirs(DUMP)
    for s in (REF, MOV):
        shutil.copy(os.path.join(SRC_TESTDATA, s), os.path.join(INPUT_DIR, s))
    print(f"[baseline] staged {REF} + {MOV}", flush=True)

    # --- capture moving_bk_dxdy (the field REG_TILE would emit) per non_rigid register() call ---
    reg_returns = []
    orig_ofw_reg = OpticalFlowWarper.register

    def cap_ofw_register(self, *a, **k):
        out = orig_ofw_reg(self, *a, **k)
        reg_returns.append(out[2])  # (warped, fwd, bk) -> bk == moving_bk_dxdy
        return out
    OpticalFlowWarper.register = cap_ofw_register

    # --- capture calc_deformation inputs + the composed self.bk_dxdy, per slide ---
    cap = {}
    orig_calc_def = snr.NonRigidZImage.calc_deformation

    def cap_calc_deformation(self, registered_fixed_image, non_rigid_reg_class,
                             bk_dxdy=None, params=None, mask=None):
        idx = len(reg_returns)
        from_rigid = self.reg_obj.from_rigid_reg
        rec = {"from_rigid_reg": bool(from_rigid),
               "incoming_bk": None if bk_dxdy is None else save_field(bk_dxdy, os.path.join(DUMP, f"{self.name}__incoming")),
               "mask": None}
        try:
            rio = self.reg_obj.src.img_obj_dict[self.name]
            rec["M"] = np.asarray(rio.M).tolist()
            rec["unwarped_shape"] = [int(x) for x in rio.image.shape[0:2]]
            rec["og_reg_shape_rc"] = [int(x) for x in rio.registered_shape_rc]
        except Exception as e:
            print(f"[baseline] no rigid img_obj for {self.name}: {type(e).__name__}: {e}", flush=True)
        if mask is not None:
            np.save(os.path.join(DUMP, f"{self.name}__regmask.npy"), np.asarray(mask))
            rec["mask"] = "saved"
        out = orig_calc_def(self, registered_fixed_image, non_rigid_reg_class,
                            bk_dxdy=bk_dxdy, params=params, mask=mask)
        rec["moving_bk_kind"] = save_field(reg_returns[idx], os.path.join(DUMP, f"{self.name}__moving_bk"))
        rec["self_bk_kind"] = save_field(self.bk_dxdy, os.path.join(DUMP, f"{self.name}__self_bk"))
        cap[self.name] = rec
        return out
    snr.NonRigidZImage.calc_deformation = cap_calc_deformation

    # --- capture the exact plain-data warp contract slide.warp_slide resolves to ---
    warp_cap = {}
    orig_st_warp = slide_tools.warp_slide

    def cap_st_warp_slide(src_f, transformation_src_shape_rc, transformation_dst_shape_rc,
                          aligned_slide_shape_rc, M=None, dxdy=None, level=0, series=None,
                          interp_method="bicubic", bbox_xywh=None, bg_color=None):
        warp_cap["kwargs"] = {
            "src_f": src_f,
            "transformation_src_shape_rc": list(transformation_src_shape_rc),
            "transformation_dst_shape_rc": list(transformation_dst_shape_rc),
            "aligned_slide_shape_rc": list(aligned_slide_shape_rc),
            "M": np.asarray(M).tolist() if M is not None else None,
            "level": int(level), "series": series, "interp_method": interp_method,
            "bbox_xywh": None if bbox_xywh is None else [int(x) for x in bbox_xywh],
            "bg_color": None if bg_color is None else [float(x) for x in bg_color],
        }
        return orig_st_warp(src_f, transformation_src_shape_rc, transformation_dst_shape_rc,
                            aligned_slide_shape_rc, M=M, dxdy=dxdy, level=level, series=series,
                            interp_method=interp_method, bbox_xywh=bbox_xywh, bg_color=bg_color)
    slide_tools.warp_slide = cap_st_warp_slide

    # --- capture the internal field pad (calc_deformation field -> nr_obj.bk_dxdy, line 3671) ---
    pad_calls = []
    orig_pad = registration.Valis.pad_displacement

    def cap_pad(self, dxdy, out_shape_rc, bbox_xywh):
        out = orig_pad(self, dxdy, out_shape_rc, bbox_xywh)
        pad_calls.append({"in_shape": list(field_to_np(dxdy).shape[:2]),
                          "out_shape": [int(out_shape_rc[0]), int(out_shape_rc[1])],
                          "bbox": [int(x) for x in bbox_xywh]})
        return out
    registration.Valis.pad_displacement = cap_pad

    reg = registration.Valis(
        INPUT_DIR, os.path.join(WORK, "valis_out"),
        reference_img_f=REF, align_to_reference=True, crop="reference",
        non_rigid_registrar_cls=OpticalFlowWarper, create_masks=True,
        micro_rigid_registrar_cls=None,
        max_processed_image_dim_px=MAX_PROCESSED_DIM,
        max_non_rigid_registration_dim_px=MAX_NON_RIGID_DIM)
    try:
        reg.register()
    finally:
        OpticalFlowWarper.register = orig_ofw_reg
        snr.NonRigidZImage.calc_deformation = orig_calc_def
        slide_tools.warp_slide = orig_st_warp
        registration.Valis.pad_displacement = orig_pad

    # register() nulls reg.non_rigid_registrar in its pre-pickle teardown (§6.1), so read the final
    # warp field off the slide instead (not_using_tiler: slide.bk_dxdy == nr_obj.bk_dxdy).
    mov_stem = MOV.replace(".ome.tiff", "").replace(".tiff", "")
    mov_name = next(k for k in reg.slide_dict if mov_stem in k)
    slide_obj = reg.slide_dict[mov_name]
    print(f"[baseline] register() done. slide_dict keys={list(reg.slide_dict)} "
          f"mov_name={mov_name} n register() calls={len(reg_returns)} pad_calls={pad_calls}", flush=True)

    nr_kind = save_field(slide_obj.bk_dxdy, os.path.join(DUMP, f"{mov_name}__nr_final"))

    # Match the internal pad (calc_deformation field -> slide field) for this slide, if any.
    self_bk_shape = list(field_to_np(load_field(os.path.join(DUMP, f"{mov_name}__self_bk"))).shape[:2])
    slide_shape = list(field_to_np(slide_obj.bk_dxdy).shape[:2])
    internal_pad = next((p for p in pad_calls
                         if p["in_shape"] == self_bk_shape and p["out_shape"] == slide_shape), None)

    reg_state = {
        "mov_name": mov_name,
        "full_displacement_shape_rc": list(getattr(reg, "_full_displacement_shape_rc", slide_shape)),
        "non_rigid_bbox": [int(x) for x in getattr(reg, "_non_rigid_bbox", [0, 0, slide_shape[1], slide_shape[0]])],
        "internal_pad": internal_pad,           # None => calc_deformation field IS the slide field
        "self_bk_shape": self_bk_shape, "slide_shape": slide_shape,
        "nr_final_kind": nr_kind,
    }

    # Produce + persist the BASELINE warped slide (ground truth for P2 / CHAIN). Re-install the
    # warp_slide wrapper around THIS call to capture the resolved plain-data contract (the finally
    # block restored it after register()).
    slide_tools.warp_slide = cap_st_warp_slide
    try:
        warped = slide_obj.warp_slide(level=0, crop=True)
    finally:
        slide_tools.warp_slide = orig_st_warp
    from valis import warp_tools
    np.save(os.path.join(DUMP, "baseline_warped.npy"), warp_tools.vips2numpy(warped))
    def _jd(o):
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return float(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
        raise TypeError(type(o))
    json.dump({"cap": cap, "reg_state": reg_state, "warp": warp_cap.get("kwargs"),
               "ref_name": os.path.splitext(REF)[0]},
              open(os.path.join(DUMP, "manifest.json"), "w"), indent=2, default=_jd)
    print(f"[baseline] dumped warp contract + fields under {DUMP}", flush=True)
    print(f"[baseline] warp kwargs: {json.dumps(warp_cap.get('kwargs'), default=str)[:400]}", flush=True)
    return mov_name


# --------------------------------------------------------------------------- REBUILD (fresh proc)
def mode_rebuild():
    import pyvips
    from valis import warp_tools, slide_tools
    man = json.load(open(os.path.join(DUMP, "manifest.json")))
    mov = man["reg_state"]["mov_name"]
    rec = man["cap"][mov]

    moving_bk = load_field(os.path.join(DUMP, f"{mov}__moving_bk"))
    classic_self_bk = load_field(os.path.join(DUMP, f"{mov}__self_bk"))
    reg_mask = (np.load(os.path.join(DUMP, f"{mov}__regmask.npy"))
                if rec["mask"] == "saved" else None)
    from_rigid = bool(rec["from_rigid_reg"])
    M = np.asarray(rec["M"]) if "M" in rec else None
    unwarped = tuple(rec["unwarped_shape"]) if "unwarped_shape" in rec else None
    og_reg = tuple(rec["og_reg_shape_rc"]) if "og_reg_shape_rc" in rec else None
    print(f"[rebuild] from_rigid_reg={from_rigid} has_mask={reg_mask is not None} "
          f"incoming={'saved' if rec['incoming_bk'] else 'identity'} "
          f"moving_bk_is_vips={isinstance(moving_bk, pyvips.Image)}", flush=True)

    # incoming/reference field: identity when align_to_reference=True. Reconstruct as the right
    # zero type so the add in compose is type-faithful.
    if rec["incoming_bk"] is not None:
        incoming = load_field(os.path.join(DUMP, f"{mov}__incoming"))
    else:
        if isinstance(moving_bk, pyvips.Image):
            incoming = pyvips.Image.black(moving_bk.width, moving_bk.height, bands=2).cast("float")
        else:
            incoming = np.array([np.zeros(field_to_np(moving_bk).shape[:2]),
                                 np.zeros(field_to_np(moving_bk).shape[:2])])

    results = {}

    # ---- P1 COMPOSE ----
    repro_self_bk = reproduce_compose(moving_bk, incoming, reg_mask, from_rigid, M, unwarped, og_reg)
    ok1, d1, m1 = exact(repro_self_bk, classic_self_bk)
    results["P1_compose"] = (ok1, d1, m1)
    print(f"[P1 compose ] equal={ok1} max|delta|={d1} {m1}", flush=True)

    # ---- P3 HANDOFF: the lossless float32 .tiff round-trip the tiler path uses (line 3717-3720).
    # Prove extract_area -> lossless tiffsave -> reload preserves the field in the bbox EXACTLY.
    nr_final = load_field(os.path.join(DUMP, f"{mov}__nr_final"))
    bbox = man["reg_state"]["non_rigid_bbox"]
    vips_nr = nr_final if isinstance(nr_final, pyvips.Image) else warp_tools.numpy2vips(np.dstack([nr_final[0], nr_final[1]]))
    cropped = vips_nr.extract_area(*bbox)
    rt = os.path.join(WORK, "rt_bk_dxdy.tiff")
    cropped.cast("float").tiffsave(rt, compression="lzw", lossless=True, tile=True, bigtiff=True)
    reloaded = pyvips.Image.new_from_file(rt)
    ok3, d3, m3 = exact(reloaded, cropped)
    results["P3_handoff"] = (ok3, d3, m3)
    print(f"[P3 handoff ] lossless={ok3} max|delta|={d3} {m3}", flush=True)

    # ---- P2 WARP (pure plain-data slide_tools.warp_slide with the classic field) ----
    base_warped = np.load(os.path.join(DUMP, "baseline_warped.npy"))
    wk = man["warp"]
    warp_dxdy = nr_final  # not_using_tiler: slide.bk_dxdy == nr_obj.bk_dxdy
    out2 = slide_tools.warp_slide(
        wk["src_f"], tuple(wk["transformation_src_shape_rc"]), tuple(wk["transformation_dst_shape_rc"]),
        tuple(wk["aligned_slide_shape_rc"]),
        M=np.asarray(wk["M"]) if wk["M"] is not None else None,
        dxdy=warp_dxdy, level=wk["level"], series=wk["series"], interp_method=wk["interp_method"],
        bbox_xywh=tuple(wk["bbox_xywh"]) if wk["bbox_xywh"] else None,
        bg_color=wk["bg_color"])
    out2_np = warp_tools.vips2numpy(out2)
    ok2 = out2_np.shape == base_warped.shape and np.array_equal(out2_np, base_warped)
    d2 = float(np.max(np.abs(out2_np.astype(np.float64) - base_warped.astype(np.float64)))) if out2_np.shape == base_warped.shape else float("nan")
    results["P2_warp"] = (ok2, d2, "" if out2_np.shape == base_warped.shape else f"shape {out2_np.shape} vs {base_warped.shape}")
    print(f"[P2 warp    ] equal={ok2} max|delta|={d2}", flush=True)

    # ---- CHAIN: moving_bk -> [P1 compose] -> [internal pad] -> [P2 warp] == classic warped ----
    chain_self_bk = reproduce_compose(moving_bk, incoming, reg_mask, from_rigid, M, unwarped, og_reg)
    ipad = man["reg_state"]["internal_pad"]
    if ipad is None:
        chain_full = chain_self_bk          # calc_deformation field already at slide resolution
    else:
        cv = chain_self_bk if isinstance(chain_self_bk, pyvips.Image) else warp_tools.numpy2vips(np.dstack([chain_self_bk[0], chain_self_bk[1]]))
        os_rc, pb = ipad["out_shape"], ipad["bbox"]
        chain_full = pyvips.Image.black(os_rc[1], os_rc[0], bands=2).cast("float").insert(cv, pb[0], pb[1])
    out_chain = slide_tools.warp_slide(
        wk["src_f"], tuple(wk["transformation_src_shape_rc"]), tuple(wk["transformation_dst_shape_rc"]),
        tuple(wk["aligned_slide_shape_rc"]),
        M=np.asarray(wk["M"]) if wk["M"] is not None else None,
        dxdy=chain_full, level=wk["level"], series=wk["series"], interp_method=wk["interp_method"],
        bbox_xywh=tuple(wk["bbox_xywh"]) if wk["bbox_xywh"] else None,
        bg_color=wk["bg_color"])
    oc_np = warp_tools.vips2numpy(out_chain)
    okc = oc_np.shape == base_warped.shape and np.array_equal(oc_np, base_warped)
    dc = float(np.max(np.abs(oc_np.astype(np.float64) - base_warped.astype(np.float64)))) if oc_np.shape == base_warped.shape else float("nan")
    results["CHAIN"] = (okc, dc, "")
    print(f"[CHAIN e2e  ] equal={okc} max|delta|={dc}", flush=True)

    print("\n" + "=" * 72)
    all_ok = all(v[0] for v in results.values())
    for k, (ok, d, m) in results.items():
        print(f"  {k:12s} {'PASS' if ok else 'FAIL'}  max|delta|={d} {m}")
    print(f"OPTION-A FINALIZE bit-identical (compose+handoff+warp+chain): {all_ok}")
    print("=" * 72, flush=True)
    return all_ok


# --------------------------------------------------------------------------- orchestrator
def mode_all():
    print("\n##### BASELINE — whole-image classic VALIS (no tiler; Blocker-3 safe) #####\n", flush=True)
    mode_baseline()
    print("\n##### REBUILD — Option-A FINALIZE from plain data (fresh process) #####\n", flush=True)
    r = subprocess.run([sys.executable, SELF, "rebuild"], capture_output=True, text=True)
    sys.stdout.write(r.stdout); sys.stderr.write(r.stderr)
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["all", "baseline", "rebuild"])
    args = ap.parse_args()
    if args.mode == "all":
        return 0 if mode_all() else 1
    if args.mode == "baseline":
        mode_baseline(); return 0
    if args.mode == "rebuild":
        return 0 if mode_rebuild() else 1


if __name__ == "__main__":
    raise SystemExit(main())
