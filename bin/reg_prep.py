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
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "utils"))

# Read-only $HOME on HPC clusters breaks numba's on-disk cache during the valis
# import (RuntimeError: '_repeat_1d': no locator available). Redirect + disable
# before importing valis (mirrors register.py).
os.environ.setdefault("NUMBA_CACHE_DIR", "/tmp/numba_cache")
os.environ["NUMBA_DISABLE_CACHING"] = "1"
# matplotlib (pulled in transitively by valis) builds a font cache under $HOME by default; on a
# read-only cluster $HOME that warns/stalls. Redirect its config + generic XDG cache to /tmp too.
os.environ.setdefault("MPLCONFIGDIR", "/tmp/mplconfig")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/xdg_cache")

import numpy as np
import pyvips
import valis_tiling
from micro_rigid_guard import install_micro_rigid_guard
from mirage_slide_reader import MirageVipsSlideReader, all_readable
from valis import registration
from valis import serial_non_rigid as snr
from valis.non_rigid_registrars import NonRigidTileRegistrar, OpticalFlowWarper
from valis.registration import CROP_REF
from valis_config import build_registrar_kwargs, maybe_init_jvm, slide_paths

_TIMINGS = {}


class _stage:
    """Record wall-clock per prep phase (spec §5.6).

    REG_PREP is the one process on the distributed path that still does whole-slide work, so it is
    the one whose cost is not obvious from the DAG. Recording it per phase means the FIRST real run
    says which loop to attack — is it the reader, the rigid stage, or the tiler-input dump — instead
    of the next optimisation being a guess. The exit is unconditional, so a phase that raises still
    reports the time it burned before failing.
    """

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        _TIMINGS[self.name] = round(time.perf_counter() - self.t0, 3)
        print(f"[reg_prep] stage {self.name}: {_TIMINGS[self.name]}s", flush=True)
        return False


def _write_timings(out_dir):
    """Dump _TIMINGS next to the prep output. Never raises: a failed write must not lose a run."""
    try:
        with open(os.path.join(out_dir, "stage_timings.json"), "w") as fh:
            json.dump(_TIMINGS, fh, indent=2)
    except OSError as exc:
        print(
            f"[reg_prep] WARNING: could not write stage_timings.json: {exc}", flush=True
        )


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
    # Registrar config — MUST mirror bin/register.py for bit-identical rigid (incl. MicroRigidRegistrar).
    ap.add_argument("--memory-mode", default="high", choices=["high", "medium", "low"])
    ap.add_argument(
        "--skip-micro-registration",
        action="store_true",
        help="if NOT set, MicroRigidRegistrar runs in the rigid stage (matches classic)",
    )
    ap.add_argument("--max-image-dim", type=int, default=4000)
    ap.add_argument(
        "--jvm-heap-gb",
        type=int,
        default=None,
        help="explicit BioFormats JVM heap (GB); default = auto-size from input size",
    )
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    # Size the BioFormats JVM heap to the inputs only if some input is NOT mirage-readable.
    #
    # This does NOT mean no JVM runs. Valis.__init__ starts one unconditionally --
    # registration.py:2083 -> slide_tools.get_img_type() -> slide_io.init_jvm() -- to probe file
    # types, before reader_cls is ever consulted. That JVM uses VALIS's small default heap and only
    # sniffs extensions, so it is not the RAM wall. The wall was BioFormats decoding a whole slide
    # into a heap sized 3*filesize+8, and THAT is what the lazy reader removes. Say "no
    # slide-scaled heap", never "JVM-free" (spec assumption A2 is false).
    heap = maybe_init_jvm(args.input_dir, override_gb=args.jvm_heap_gb)
    print(
        f"[reg_prep] JVM heap = {heap} GB"
        if heap
        else "[reg_prep] all inputs readable by MirageVipsSlideReader; no slide-scaled JVM heap "
        "(Valis still starts a default-heap JVM to probe file types)",
        flush=True,
    )

    # Make rigid-stage micro alignment robust to featureless input (no-op on real data); see module.
    install_micro_rigid_guard()
    # Force the processed-2-D non-rigid prep branch (no tiler), then no-op the warper => no DeepFlow.
    registration.TILER_THRESH_GB = 10**9
    cap = {}  # slide_name -> {moving, fixed, mask, from_rigid, incoming_is_none, reg_mask}
    cur = {}

    orig_calc = snr.NonRigidZImage.calc_deformation

    def cap_calc(
        self,
        registered_fixed_image,
        non_rigid_reg_class,
        bk_dxdy=None,
        params=None,
        mask=None,
    ):
        cur["name"] = self.name
        cap[self.name] = {
            "from_rigid": bool(self.reg_obj.from_rigid_reg),
            "incoming_is_none": bk_dxdy is None,
            "reg_mask": None if mask is None else np.asarray(mask),
        }
        return orig_calc(
            self,
            registered_fixed_image,
            non_rigid_reg_class,
            bk_dxdy=bk_dxdy,
            params=params,
            mask=mask,
        )

    orig_reg = OpticalFlowWarper.register

    def noop_register(self, moving_img, fixed_img, mask=None, **kw):
        nm = cur.get("name")
        assert nm is not None, (
            "OpticalFlowWarper.register fired before calc_deformation set the slide name"
        )
        cap.setdefault(nm, {})
        cap[nm].update({"moving": moving_img, "fixed": fixed_img, "mask": mask})
        if isinstance(moving_img, pyvips.Image):
            h, w = moving_img.height, moving_img.width
        else:
            h, w = moving_img.shape[0:2]
        z = [
            np.zeros((h, w)),
            np.zeros((h, w)),
        ]  # numpy [dx,dy] no-op field (skips DeepFlow)
        return moving_img, z, z

    # Build the IDENTICAL Valis config as classic register.py (shared builder), so rigid (incl.
    # MicroRigidRegistrar, SuperPoint/SuperGlue) is bit-identical. No-op only the non-rigid warper.
    kwargs = build_registrar_kwargs(
        reference_img_f=args.reference,
        memory_mode=args.memory_mode,
        skip_micro_registration=args.skip_micro_registration,
        max_image_dim_px=args.max_image_dim,
    )
    snr.NonRigidZImage.calc_deformation = cap_calc
    OpticalFlowWarper.register = noop_register
    try:
        with _stage("load"):
            reg = registration.Valis(args.input_dir, args.out, **kwargs)
            reader_cls = (
                MirageVipsSlideReader
                if all_readable(slide_paths(args.input_dir))
                else None
            )
        with _stage("rigid_and_prep"):
            reg.register(reader_cls=reader_cls)
    finally:
        snr.NonRigidZImage.calc_deformation = orig_calc
        OpticalFlowWarper.register = orig_reg

    # Stem used to MATCH reg.slide_dict keys (bare basename, no dir, no extension). VALIS keys slides
    # by basename stem, but --reference is a path ("ref/<name>.ome.tif") so it must be basename'd first;
    # otherwise "ref/<name>" never equals slide_dict's "<name>" and the reference is miscounted as a
    # dropped moving slide (and its warp_state lands in an <out>/ref/ subdir). Keep args.reference (the
    # full path) untouched — VALIS needs it to locate the file.
    ref_basename = os.path.basename(args.reference)
    ref_stem = (
        ref_basename.replace(".ome.tiff", "")
        .replace(".ome.tif", "")
        .replace(".tiff", "")
        .replace(".tif", "")
    )
    ref_slide = reg.get_ref_slide()
    full_disp = [int(x) for x in reg._full_displacement_shape_rc]
    non_rigid_bbox = [int(x) for x in reg._non_rigid_bbox]
    # CROP_REF bbox at level 0 (warp_slide lines 882-889)
    scaled_aligned_rc = list(ref_slide.slide_dimensions_wh[0][::-1])

    def _warp_state(slide_obj, name, internal_pad=None, from_rigid=False, rec=None):
        slide_bbox_xywh, _ = slide_obj.get_crop_xywh(
            crop=CROP_REF, out_shape_rc=scaled_aligned_rc
        )
        ws = {
            "slide_name": name,
            "src_f": slide_obj.src_f,
            "from_rigid_reg": from_rigid,
            "M": np.asarray(slide_obj.M).tolist(),
            "processed_img_shape_rc": [
                int(x) for x in slide_obj.processed_img_shape_rc
            ],
            "reg_img_shape_rc": [int(x) for x in slide_obj.reg_img_shape_rc],
            "aligned_slide_shape_rc": [
                int(x) for x in slide_obj.aligned_slide_shape_rc
            ],
            "bbox_xywh": [int(x) for x in slide_bbox_xywh],
            "bg_color": [float(x) for x in slide_obj.bg_color]
            if slide_obj.bg_color is not None
            else None,
            "series": slide_obj.series,
            "is_rgb": bool(slide_obj.is_rgb),
            "interp_method": "bicubic",
        }
        if internal_pad is not None:
            ws["internal_pad"] = internal_pad
        return ws

    # Reference slide: warped-to-itself (rigid M + crop, identity non-rigid) so downstream QC has it in
    # the same cropped coordinate space as the moving slides (classic VALIS warps all slides incl. ref).
    # Use the ref Slide object VALIS already resolved (ref_slide), not a second slide_dict[ref_stem]
    # lookup that silently yields None if the stem key ever diverges. The output DIR stays ref_stem
    # because the adapter keys the reference by slideStem(ref_file) (== ref_stem).
    if ref_slide is not None:
        rdir = os.path.join(args.out, ref_stem)
        os.makedirs(rdir, exist_ok=True)
        with open(os.path.join(rdir, "warp_state.json"), "w") as fh:
            json.dump(_warp_state(ref_slide, ref_stem), fh, indent=2, default=_jd)
        print(
            f"[reg_prep] dumped REFERENCE {ref_stem} warp_state (rigid-only)",
            flush=True,
        )
    else:
        raise RuntimeError(
            "[reg_prep] no reference slide resolved by VALIS (reg.get_ref_slide() is None)"
        )

    slides_written = []
    with _stage("dump"):
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
                t = NonRigidTileRegistrar(
                    tile_wh=args.tile_wh, tile_buffer=args.tile_buffer
                )
                t.register(
                    rec["moving"],
                    rec["fixed"],
                    mask=rec["mask"],
                    non_rigid_registrar_cls=OpticalFlowWarper,
                    processing_cls=None,
                    processing_kwargs=None,
                    target_stats=None,
                )
            except valis_tiling.TilesPending:
                pass
            finally:
                valis_tiling.clear_hook()

            if rec["reg_mask"] is not None:
                np.save(os.path.join(tiler_inputs, "reg_mask.npy"), rec["reg_mask"])

            ws = _warp_state(
                slide_obj,
                name,
                internal_pad={"out_shape": full_disp, "bbox": non_rigid_bbox},
                from_rigid=rec["from_rigid"],
                rec=rec,
            )
            with open(os.path.join(sdir, "warp_state.json"), "w") as fh:
                json.dump(ws, fh, indent=2, default=_jd)
            slides_written.append(name)
            print(
                f"[reg_prep] dumped {name}: tiler_inputs + warp_state "
                f"(reg_shape={ws['reg_img_shape_rc']} bbox={ws['bbox_xywh']})",
                flush=True,
            )

    # Written BEFORE the loss guard below, so a run that fails it still says where the time went.
    _write_timings(args.out)

    # Guard against silent slide loss: every non-reference slide VALIS registered MUST have produced a
    # tiler_inputs+warp_state dump. If capture didn't fire for one (name mismatch, swallowed error), it
    # would otherwise be omitted while the process still exits 0 — a missing registered output presented
    # as a clean run. Fail loudly instead.
    expected_moving = [name for name in reg.slide_dict if name != ref_stem]
    missing = [name for name in expected_moving if name not in slides_written]
    if missing:
        if heap:
            registration.kill_jvm()
        raise RuntimeError(
            f"[reg_prep] {len(missing)} moving slide(s) produced no tiler_inputs/warp_state and were "
            f"silently dropped: {missing} (expected {len(expected_moving)}, wrote {len(slides_written)}). "
            "The non-rigid warper capture did not fire for those slides."
        )

    if heap:
        registration.kill_jvm()
    print(
        f"[reg_prep] DONE — {len(slides_written)} moving slide(s): {slides_written} "
        f"timings={_TIMINGS}",
        flush=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
