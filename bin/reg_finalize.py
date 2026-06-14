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


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--warp-state", required=True, help="REG_PREP per-slide warp_state.json")
    ap.add_argument("--src-slide", required=True, help="full-res source ome.tiff to warp")
    ap.add_argument("--out", required=True, help="output ome.tiff path")
    ap.add_argument("--inputs-dir", help="REG_PREP tiler_inputs (manifest.json + expanded_bboxes.npy)")
    ap.add_argument("--tiles-dir", help="REG_TILE output (bk_*.v / fwd_*.v)")
    ap.add_argument("--rigid-only", action="store_true",
                    help="reference slide: warp with rigid M + crop only (dxdy=None), no tiles/compose")
    ap.add_argument("--compression", default="lzw")
    args = ap.parse_args()
    ws = json.load(open(args.warp_state))

    if args.rigid_only:
        # Reference (or any rigid-only) slide: VALIS warps it with its M + crop and identity non-rigid.
        slide_bk = None
    else:
        if not args.inputs_dir or not args.tiles_dir:
            raise SystemExit("--inputs-dir and --tiles-dir are required unless --rigid-only")
        m = json.load(open(os.path.join(args.inputs_dir, "manifest.json")))
        expanded_bboxes = np.load(os.path.join(args.inputs_dir, "expanded_bboxes.npy"))

        # 1) stitch the per-tile fields back into the full non-rigid field (== VALIS calc(), §6.1)
        n = m["n_tiles"]
        bk_tiles = [pyvips.Image.new_from_file(os.path.join(args.tiles_dir, f"bk_{i}.v")) for i in range(n)]
        moving_bk = warp_tools.stitch_tiles(bk_tiles, expanded_bboxes, m["n_rows"], m["n_cols"], m["tile_buffer"])

        # 2) compose (§6.3). align_to_reference=True => incoming/reference field is identity.
        reg_mask_f = os.path.join(args.inputs_dir, "reg_mask.npy")
        reg_mask = np.load(reg_mask_f) if os.path.exists(reg_mask_f) else None
        incoming = pyvips.Image.black(moving_bk.width, moving_bk.height, bands=2).cast("float")
        self_bk = compose(
            moving_bk, incoming, reg_mask,
            from_rigid_reg=ws.get("from_rigid_reg", False),
            M=np.asarray(ws["M"]) if ws.get("M") is not None else None,
            unwarped_shape=tuple(ws["unwarped_shape"]) if ws.get("unwarped_shape") else None,
            og_reg_shape_rc=tuple(ws["og_reg_shape_rc"]) if ws.get("og_reg_shape_rc") else None,
        )

        # 3) pad the composed (masked/scaled) field up to the full registered resolution (line 3671)
        ipad = ws.get("internal_pad")
        if ipad is None:
            slide_bk = self_bk
        else:
            os_rc, pb = ipad["out_shape"], ipad["bbox"]
            slide_bk = pyvips.Image.black(os_rc[1], os_rc[0], bands=2).cast("float").insert(
                _to_vips_field(self_bk), pb[0], pb[1])

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
        # OME-XML serialization is environment-fragile (ome-types/OMEConverter). The warped PIXELS are
        # correct (proven §6.3) regardless; fall back to a pixel-faithful OME-TIFF and warn so the
        # pipeline completes. Full channel-metadata parity is tracked as a finishing item (Task 10).
        print(f"[reg_finalize] WARNING ome-xml path failed ({type(e).__name__}: {e}); "
              f"writing pixel-faithful fallback", flush=True)
        warped.tiffsave(args.out, compression=args.compression, tile=True, tile_width=tile_wh,
                        tile_height=tile_wh, bigtiff=True, pyramid=True)
    print(f"[reg_finalize] wrote {args.out} ({warped.width}x{warped.height} bands={warped.bands})", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
