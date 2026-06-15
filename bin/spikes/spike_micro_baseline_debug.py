#!/usr/bin/env python3
"""Debug why the verify baseline warp returns 1 band. Prints field type/shape, stored_dxdy, source
bands, and warp result bands — with the SAME build_registrar_kwargs config as the distributed chain."""
import os
import shutil
import sys

import numpy as np
import pyvips

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "utils"))
from valis_config import build_registrar_kwargs
from valis import registration, warp_tools, slide_tools, slide_io
from valis.non_rigid_registrars import OpticalFlowWarper
from valis.registration import CROP_REF

WORK, INP = "/tmp/micro_dbg", "/tmp/micro_dbg/in"
DATADIR = os.environ.get("CMP_DATADIR", "/tmp/bigdata")
REF, MOV, MOV_STEM = "P001_ref.ome.tiff", "P001_mov1.ome.tiff", "P001_mov1"


def bands(x):
    if isinstance(x, pyvips.Image):
        return f"vips bands={x.bands} {x.width}x{x.height}"
    a = np.asarray(x)
    return f"numpy shape={a.shape}"


def main():
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(INP)
    for s in (REF, MOV):
        shutil.copy(os.path.join(DATADIR, s), os.path.join(INP, s))

    print("source mov bands:", bands(pyvips.Image.new_from_file(os.path.join(INP, MOV))), flush=True)

    registration.TILER_THRESH_GB = 10
    kwargs = build_registrar_kwargs(reference_img_f=REF, memory_mode="low", skip_micro_registration=False)
    reg = registration.Valis(INP, os.path.join(WORK, "out"), **kwargs)
    reg.register()
    s = reg.slide_dict[MOV_STEM]
    print(f"after register(): bk_dxdy={bands(s.bk_dxdy)} stored_dxdy={s.stored_dxdy} series={s.series}", flush=True)

    img_dims = np.array([sl.slide_dimensions_wh[0] for sl in reg.slide_dict.values()])
    micro_size = int(np.floor(np.min([np.max(d) for d in img_dims]) * 0.125))
    reg.register_micro(max_non_rigid_registration_dim_px=micro_size, reference_img_f=REF,
                       align_to_reference=True, tile_wh=2048)
    s = reg.slide_dict[MOV_STEM]
    print(f"after register_micro(): bk_dxdy={bands(s.bk_dxdy)} stored_dxdy={s.stored_dxdy} "
          f"_bk_dxdy_f={getattr(s, '_bk_dxdy_f', None)}", flush=True)

    # reader path used by warp_slide
    reader_cls = slide_io.get_slide_reader(os.path.join(INP, MOV), series=s.series)
    reader = reader_cls(os.path.join(INP, MOV), series=s.series)
    vips_slide = reader.slide2vips(level=0, series=s.series if s.series is not None else reader.series)
    print("reader.slide2vips bands:", bands(vips_slide), flush=True)

    bbox, _ = s.get_crop_xywh(crop=CROP_REF, out_shape_rc=list(s.slide_dimensions_wh[0][::-1]))
    warped = slide_tools.warp_slide(
        os.path.join(INP, MOV), transformation_src_shape_rc=tuple(s.processed_img_shape_rc),
        transformation_dst_shape_rc=tuple(s.reg_img_shape_rc),
        aligned_slide_shape_rc=tuple(s.aligned_slide_shape_rc), M=np.asarray(s.M),
        dxdy=s.bk_dxdy, level=0, series=s.series, interp_method="bicubic",
        bbox_xywh=tuple(int(x) for x in bbox), bg_color=s.bg_color)
    print("warp_slide(s.bk_dxdy) result:", bands(warped), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
