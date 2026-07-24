#!/usr/bin/env python3
"""REG_NONRIGID (spec §6.7): whole-image non-rigid registration in a JVM-FREE process.

The user's RAM hotspot is the BioFormats JVM heap held *during* the non-rigid pass. The non-rigid
pass itself (OpticalFlowWarper / DeepFlow) needs no JVM — just pyvips + OpenCV. So running it
whole-image in a separate JVM-free process removes the JVM from the non-rigid stage WITHOUT tiling,
i.e. it is **bit-identical to classic** (same OpticalFlowWarper on the same 2-D inputs that REG_PREP
captured, deterministic) while solving the RAM spike. This is the default distributed non-rigid step;
the REG_TILE fan-out is only the fallback for a single field too big for one node (est_GB > threshold).

Reads REG_PREP's dump (moving.v, fixed.v, mask.v?, manifest.json), runs OpticalFlowWarper whole-image,
emits the backward field bk.v (== classic's `moving_bk_dxdy`, the input to REG_FINALIZE's compose).
"""

import argparse
import json
import os

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
from valis import warp_tools
from valis.non_rigid_registrars import OpticalFlowWarper


def _save_field(field, path):
    # CONTRACT: bk.v is persisted as float32 (pyvips "float"). This is intentional and verified:
    #   - the field-mode FINALIZE (reg_finalize.py) composes+warps directly from this float32 field
    #     (bit-identical to classic for the wave-1-only final warp);
    #   - the micro path (reg_micro_prep._vips_to_numpy_pair) reloads and recasts to float64 before
    #     injection (float32 injection diverges at high res) — that recast is the single source of
    #     truth for micro. Any new consumer that is dtype-sensitive MUST recast, like the micro path.
    if isinstance(field, pyvips.Image):
        field.write_to_file(path)
    else:
        warp_tools.numpy2vips(
            np.dstack([np.asarray(field[0]), np.asarray(field[1])])
        ).cast("float").write_to_file(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--inputs-dir",
        required=True,
        help="REG_PREP tiler_inputs (moving.v, fixed.v, mask.v?, manifest.json)",
    )
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    m = json.load(open(os.path.join(args.inputs_dir, "manifest.json")))

    def load2d(name):
        # OpticalFlowWarper.register does numpy ops/slicing -> feed the SAME 2-D numpy classic feeds it
        # (the processed image). REG_PREP's .v are single-band; squeeze to (H, W).
        arr = warp_tools.vips2numpy(
            pyvips.Image.new_from_file(os.path.join(args.inputs_dir, name))
        )
        return np.squeeze(arr)

    moving = load2d("moving.v")
    fixed = load2d("fixed.v")
    mask = load2d("mask.v") if m["has_mask"] else None

    reg = OpticalFlowWarper()
    _, _, bk_dxdy = reg.register(
        moving_img=moving, fixed_img=fixed, mask=mask
    )  # whole-image DeepFlow

    os.makedirs(args.out_dir, exist_ok=True)
    _save_field(bk_dxdy, os.path.join(args.out_dir, "bk.v"))
    print(
        f"[reg_nonrigid] whole-image field -> {args.out_dir}/bk.v (pid={os.getpid()}, no JVM)",
        flush=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
