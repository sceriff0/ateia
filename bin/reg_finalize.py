#!/usr/bin/env python3
"""REG_FINALIZE (Option A, spec §6.3/§6.6): distributed-tiling stage 3.

Stitch the precomputed per-tile displacement fields (from REG_TILE) into the full non-rigid field,
reproduce VALIS's post-tiler composition (serial_non_rigid.calc_deformation 460-503), pad to the
full registered resolution, and warp+save the full-res slide via VALIS's own streaming
``slide_tools.warp_slide`` — all from PLAIN dumped data, no pickled ``Valis`` registrar (Blocker 1).

The compose + warp legs are proven pixel-identical to classic by ``bin/spikes/spike_finalize_option_a.py``
(``max|Δ|=0``). Reads REG_PREP's dump: ``<inputs-dir>/manifest.json`` + ``expanded_bboxes.npy`` (tile
grid), ``<tiles-dir>/bk_*.v`` & ``fwd_*.v`` (REG_TILE output), ``<warp-state>`` JSON (per-slide plain
warp state), and an optional ``reg_mask.npy``.
"""
import argparse
import json
import os
import sys

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

from valis import warp_tools, slide_tools, slide_io


# --------------------------------------------------------------------------- compose (proven §6.3)
def _mask_img(img, mask):
    if isinstance(img, pyvips.Image):
        vmask = mask if isinstance(mask, pyvips.Image) else warp_tools.numpy2vips(np.asarray(mask))
        return (vmask == 0).ifthenelse(0, img)
    out = img.copy()
    out[np.asarray(mask) == 0] = 0
    return out


def _mask_dxdy(dxdy, mask):
    if isinstance(dxdy, pyvips.Image):
        return _mask_img(dxdy, mask)
    return [_mask_img(dxdy[0], mask), _mask_img(dxdy[1], mask)]


def compose(moving_bk_dxdy, incoming_bk_dxdy, reg_mask, from_rigid_reg=False,
            M=None, unwarped_shape=None, og_reg_shape_rc=None):
    """Port of serial_non_rigid.NonRigidZImage.calc_deformation 460-503 (spec §6.3). In production
    ``from_rigid_reg`` is False (scaled-image non-rigid), so remove_invasive_displacements is skipped."""
    if from_rigid_reg:  # 460-465
        moving_bk_dxdy = warp_tools.remove_invasive_displacements(
            moving_bk_dxdy, M=M, src_shape_rc=unwarped_shape, out_shape_rc=og_reg_shape_rc)

    if not isinstance(moving_bk_dxdy, pyvips.Image):  # 467-472 numpy branch
        if reg_mask is not None:
            moving_bk_dxdy = _mask_dxdy(moving_bk_dxdy, reg_mask)
        bk_dxdy_from_ref = np.array([incoming_bk_dxdy[0] + moving_bk_dxdy[0],
                                     incoming_bk_dxdy[1] + moving_bk_dxdy[1]])
    else:  # 473-476 pyvips branch
        if reg_mask is not None:
            moving_bk_dxdy = _mask_dxdy(moving_bk_dxdy, reg_mask)
        bk_dxdy_from_ref = incoming_bk_dxdy + moving_bk_dxdy

    img_bk_dxdy = bk_dxdy_from_ref.copy() if hasattr(bk_dxdy_from_ref, "copy") else bk_dxdy_from_ref
    if reg_mask is not None:  # 479-480
        img_bk_dxdy = _mask_dxdy(img_bk_dxdy, reg_mask)
    if from_rigid_reg:  # 482-487
        img_bk_dxdy = warp_tools.remove_invasive_displacements(
            img_bk_dxdy, M=M, src_shape_rc=unwarped_shape, out_shape_rc=og_reg_shape_rc)
    return img_bk_dxdy


def _to_vips_field(field):
    if isinstance(field, pyvips.Image):
        return field
    return warp_tools.numpy2vips(np.dstack([np.asarray(field[0]), np.asarray(field[1])])).cast("float")


def compose_and_pad(moving_bk, ws, reg_mask):
    """Reproduce classic ``slide.bk_dxdy`` after ``register()`` (registration.py:3707): the §6.3
    compose of the raw non-rigid field, then internal_pad up to ``_full_displacement_shape_rc``.
    Proven == classic by spike_finalize_option_a + the micro oracle (Q1: shape == full_disp)."""
    incoming = pyvips.Image.black(moving_bk.width, moving_bk.height, bands=2).cast("float")
    self_bk = compose(
        moving_bk, incoming, reg_mask,
        from_rigid_reg=ws.get("from_rigid_reg", False),
        M=np.asarray(ws["M"]) if ws.get("M") is not None else None,
        unwarped_shape=tuple(ws["unwarped_shape"]) if ws.get("unwarped_shape") else None,
        og_reg_shape_rc=tuple(ws["og_reg_shape_rc"]) if ws.get("og_reg_shape_rc") else None,
    )
    ipad = ws.get("internal_pad")
    if ipad is None:
        return _to_vips_field(self_bk)
    os_rc, pb = ipad["out_shape"], ipad["bbox"]
    return pyvips.Image.black(os_rc[1], os_rc[0], bands=2).cast("float").insert(
        _to_vips_field(self_bk), pb[0], pb[1])


def micro_additive(slide_bk_full, micro_raw_bk, micro_ws, micro_reg_mask):
    """Reproduce register_micro's additive field update (registration.py:4299-4330), proven
    max|Δ|=0 by spike_micro_option2.py. Returns the micro-updated backward field at micro
    ``full_out_shape_rc`` — the field the final warp consumes.

      updated = scale(slide_bk_full -> micro_out_shape) + pad(compose(micro_raw) -> micro_full_out @ bbox)
    """
    full_out = tuple(micro_ws["micro_full_out_shape_rc"])          # e.g. (129, 129)
    mask_bbox = micro_ws.get("micro_mask_bbox_xywh")               # [x, y, w, h]

    # 1) compose the micro residual (== nr_obj.bk_dxdy post calc_deformation). For micro,
    #    align_to_reference=True => incoming is identity; mask/from_rigid per the micro warp_state.
    incoming = pyvips.Image.black(micro_raw_bk.width, micro_raw_bk.height, bands=2).cast("float")
    composed_residual = _to_vips_field(compose(
        micro_raw_bk, incoming, micro_reg_mask,
        from_rigid_reg=micro_ws.get("from_rigid_reg", False)))

    # 2) pad the residual into the micro full_out field at mask_bbox (registration.py:4312-4314)
    if (composed_residual.height, composed_residual.width) != full_out:
        bx, by = (mask_bbox[0], mask_bbox[1]) if mask_bbox else (0, 0)
        vips_new_bk = pyvips.Image.black(full_out[1], full_out[0], bands=2).cast("float").insert(
            composed_residual, bx, by)
    else:
        vips_new_bk = composed_residual

    # 3) scale the wave-1 field to the micro out_shape (registration.py:4317-4323)
    cur = _to_vips_field(slide_bk_full)
    slide_sxy = (np.array(full_out) / np.array([cur.height, cur.width]))[::-1]
    if not np.all(slide_sxy == 1):
        sx, sy = float(slide_sxy[0]), float(slide_sxy[1])
        cur = warp_tools.resize_img((sx * cur[0]).bandjoin(sy * cur[1]), full_out)

    # 4) additive (registration.py:4330)
    return cur + vips_new_bk


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--warp-state", required=True, help="REG_PREP per-slide warp_state.json")
    ap.add_argument("--src-slide", required=True, help="full-res source ome.tiff to warp")
    ap.add_argument("--out", required=True, help="output ome.tiff path")
    ap.add_argument("--inputs-dir", help="REG_PREP tiler_inputs (manifest.json + expanded_bboxes.npy)")
    ap.add_argument("--tiles-dir", help="REG_TILE output (bk_*.v / fwd_*.v)")
    ap.add_argument("--field", help="whole-image non-rigid field bk.v from REG_NONRIGID (separated mode)")
    ap.add_argument("--rigid-only", action="store_true",
                    help="reference slide: warp with rigid M + crop only (dxdy=None), no tiles/compose")
    # Micro-registration second wave (spec §5A Option-2): additively compose the micro residual.
    ap.add_argument("--micro-field", help="whole-image MICRO residual bk.v from REG_NONRIGID on the "
                    "micro 2-D inputs (REG_MICRO_PREP); triggers the additive micro compose")
    ap.add_argument("--micro-warp-state", help="REG_MICRO_PREP micro_warp_state.json "
                    "(micro_full_out_shape_rc, micro_mask_bbox_xywh, from_rigid_reg)")
    ap.add_argument("--micro-inputs-dir", help="REG_MICRO_PREP micro tiler_inputs (optional reg_mask.npy)")
    ap.add_argument("--compression", default="lzw")
    args = ap.parse_args()
    ws = json.load(open(args.warp_state))

    if args.rigid_only:
        # Reference (or any rigid-only) slide: VALIS warps it with its M + crop and identity non-rigid.
        slide_bk = None
    else:
        # Obtain moving_bk: either the whole-image field (separated mode, == classic) or by stitching
        # the per-tile fields (tiled mode, == VALIS calc()). Both feed the SAME §6.3 compose+warp.
        if args.field:
            moving_bk = pyvips.Image.new_from_file(args.field)
        else:
            if not args.inputs_dir or not args.tiles_dir:
                raise SystemExit("provide --field, or --inputs-dir + --tiles-dir, or --rigid-only")
            m = json.load(open(os.path.join(args.inputs_dir, "manifest.json")))
            expanded_bboxes = np.load(os.path.join(args.inputs_dir, "expanded_bboxes.npy"))
            n = m["n_tiles"]
            bk_tiles = [pyvips.Image.new_from_file(os.path.join(args.tiles_dir, f"bk_{i}.v")) for i in range(n)]
            moving_bk = warp_tools.stitch_tiles(bk_tiles, expanded_bboxes, m["n_rows"], m["n_cols"], m["tile_buffer"])

        # compose (§6.3) + internal_pad => classic slide.bk_dxdy after register() (the wave-1 field).
        # align_to_reference=True => incoming/reference field is identity.
        reg_mask_f = os.path.join(args.inputs_dir, "reg_mask.npy") if args.inputs_dir else None
        reg_mask = np.load(reg_mask_f) if reg_mask_f and os.path.exists(reg_mask_f) else None
        slide_bk = compose_and_pad(moving_bk, ws, reg_mask)

        # Micro second wave (spec §5A Option-2): additively compose the micro residual onto the
        # wave-1 field, reproducing register_micro (registration.py:4299-4330). The result is the
        # micro-resolution field the final warp consumes (warp_slide resizes it to reg_img_shape).
        if args.micro_field:
            micro_ws = json.load(open(args.micro_warp_state))
            micro_raw_bk = pyvips.Image.new_from_file(args.micro_field)
            micro_mask_f = os.path.join(args.micro_inputs_dir, "reg_mask.npy") if args.micro_inputs_dir else None
            micro_reg_mask = np.load(micro_mask_f) if micro_mask_f and os.path.exists(micro_mask_f) else None
            slide_bk = micro_additive(slide_bk, micro_raw_bk, micro_ws, micro_reg_mask)

    # 4) warp the full-res slide via VALIS's own streaming warp (pure plain-data, §6.3)
    warped = slide_tools.warp_slide(
        args.src_slide,
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

    # 5) save as OME-TIFF, mirroring Slide.warp_and_save_slide's metadata handling (registration.py:982-1024)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    reader_cls = slide_io.get_slide_reader(args.src_slide, series=ws.get("series"))
    reader = reader_cls(args.src_slide, series=ws.get("series"))
    slide_meta = reader.metadata
    out_xyczt = slide_io.get_shape_xyzct((warped.width, warped.height), warped.bands)
    tile_wh = slide_meta.optimal_tile_wh
    if min(out_xyczt[0:2]) < tile_wh:
        tile_wh = min(out_xyczt[0:2])
    try:
        px_phys = None if slide_meta.pixel_physical_size_xyu[2] == slide_io.PIXEL_UNIT else reader.scale_physical_size(0)
        bf_dtype = slide_io.vips2bf_dtype(warped.format)
        ome_xml = slide_io.update_xml_for_new_img(
            current_ome_xml_str=slide_meta.original_xml, new_xyzct=out_xyczt, bf_dtype=bf_dtype,
            is_rgb=ws.get("is_rgb", False), series=ws.get("series"),
            pixel_physical_size_xyu=px_phys, channel_names=slide_meta.channel_names, colormap=None).to_xml()
        slide_io.save_ome_tiff(warped, dst_f=args.out, ome_xml=ome_xml, tile_wh=tile_wh, compression=args.compression)
    except Exception as e:
        # VALIS's update_xml_for_new_img uses ome_types.from_xml(parser="xmlschema"), which is
        # environment-fragile (needs the OME schema; classic warp_and_save_slide hits the same path).
        # The warped PIXELS are correct (proven §6.3); fall back to a hand-built minimal OME-XML that
        # still carries the channel NAMES, so downstream (segmentation/quant) keeps correct channels.
        print(f"[reg_finalize] WARNING update_xml path failed ({type(e).__name__}: {e}); "
              f"writing minimal OME-XML with channel names", flush=True)
        chan_names = list(getattr(slide_meta, "channel_names", None) or [f"C{i}" for i in range(warped.bands)])
        if len(chan_names) < warped.bands:
            chan_names += [f"C{i}" for i in range(len(chan_names), warped.bands)]
        # slide_io.save_ome_tiff ALSO routes through ome_types (same crash), so write the OME-TIFF
        # directly with pyvips (channel names embedded), bypassing the fragile dependency.
        _save_ome_pyvips(warped, args.out, chan_names[:warped.bands], slide_io.vips2bf_dtype(warped.format),
                         tile_wh, args.compression)
    print(f"[reg_finalize] wrote {args.out} ({warped.width}x{warped.height} bands={warped.bands})", flush=True)


def _save_ome_pyvips(warped, dst_f, channel_names, bf_dtype, tile_wh, compression):
    """Write a multi-channel OME-TIFF via pyvips (libvips OME convention), embedding channel names.
    Avoids slide_io.save_ome_tiff / ome_types (environment-fragile). The C bands are stacked
    vertically into pages (page-height = H) and the OME-XML declares SizeC with DimensionOrder XYCZT."""
    c = warped.bands
    ome_xml = _minimal_ome_xml(warped.width, warped.height, c, bf_dtype, channel_names)
    if c == 1:
        page = warped
    else:
        bands = [warped[i] for i in range(c)]
        page = bands[0]
        for b in bands[1:]:
            page = page.join(b, "vertical")
    page = page.copy()
    page.set_type(pyvips.GValue.gint_type, "page-height", warped.height)
    page.set_type(pyvips.GValue.gstr_type, "image-description", ome_xml)
    tw = max(16, min(tile_wh, warped.width, warped.height))
    page.tiffsave(dst_f, compression=compression, tile=True, tile_width=tw, tile_height=tw, bigtiff=True)


def _minimal_ome_xml(w, h, c, bf_dtype, channel_names):
    """Build a minimal valid OME-XML (no ome_types) preserving channel names + dims, so registered
    slides keep channel metadata even when VALIS's xmlschema-based xml builder fails in-environment."""
    import html
    chans = "".join(
        f'<Channel ID="Channel:0:{i}" Name="{html.escape(str(n))}" SamplesPerPixel="1"/>'
        for i, n in enumerate(channel_names))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xsi:schemaLocation="http://www.openmicroscopy.org/Schemas/OME/2016-06 '
        'http://www.openmicroscopy.org/Schemas/OME/2016-06/ome.xsd">'
        '<Image ID="Image:0" Name="registered">'
        f'<Pixels ID="Pixels:0" DimensionOrder="XYCZT" Type="{bf_dtype}" '
        f'Interleaved="false" SizeX="{w}" SizeY="{h}" SizeC="{c}" SizeZ="1" SizeT="1">'
        f'{chans}'
        '</Pixels></Image></OME>')


if __name__ == "__main__":
    raise SystemExit(main())
