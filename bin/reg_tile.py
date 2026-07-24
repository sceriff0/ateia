#!/usr/bin/env python3
"""REG_TILE (distributed tiling stage 2, fan-out): compute ONE tile's bk/fwd displacement field
using VALIS 1.0.0's own ``NonRigidTileRegistrar.reg_tile`` kernel, in a fresh, JVM-free process
(~1-2 GB). Bit-identical to the in-process threaded loop (max|Δ|=0).

Reads the dump contract written by ``bin/utils/valis_tiling._dump_inputs`` (via REG_PREP's halt
hook): ``moving.v``, ``fixed.v``, ``mask.v`` (if any), ``target_stats.npy`` (if any),
``expanded_bboxes.npy``, ``manifest.json``. Runs with ``processing_cls=None`` (the deterministic
path) because PREP already processed the images to their 2-D form.
Emits ``bk_<idx>.v`` / ``fwd_<idx>.v`` for the requested tile.
"""

import argparse
import importlib
import json
import logging
import os
import sys
import threading

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
from logger import configure_logging, get_logger
from valis.non_rigid_registrars import NonRigidTileRegistrar

logger = get_logger(__name__)


def _load_cls(path):
    """Re-import a 'module:QualName' class reference dumped by valis_tiling."""
    if not path:
        return None
    module, qualname = path.split(":")
    obj = importlib.import_module(module)
    for part in qualname.split("."):
        obj = getattr(obj, part)
    return obj


class _NullPbar:
    def update(self, n):
        pass


def main():
    """CLI entry point: compute one tile's displacement field and write it to the output dir."""
    configure_logging(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--inputs-dir",
        required=True,
        help="dir with moving.v, fixed.v, mask.v?, expanded_bboxes.npy, manifest.json",
    )
    ap.add_argument("--tile-idx", type=int, required=True)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    m = json.load(open(os.path.join(args.inputs_dir, "manifest.json")))
    if not (0 <= args.tile_idx < m["n_tiles"]):
        raise SystemExit(f"--tile-idx {args.tile_idx} out of range [0, {m['n_tiles']})")

    reg = NonRigidTileRegistrar(tile_wh=m["tile_wh"], tile_buffer=m["tile_buffer"])
    reg.moving_img = pyvips.Image.new_from_file(
        os.path.join(args.inputs_dir, "moving.v")
    )
    reg.fixed_img = pyvips.Image.new_from_file(os.path.join(args.inputs_dir, "fixed.v"))
    reg.mask = (
        pyvips.Image.new_from_file(os.path.join(args.inputs_dir, "mask.v"))
        if m["has_mask"]
        else None
    )
    reg.expanded_bboxes = np.load(os.path.join(args.inputs_dir, "expanded_bboxes.npy"))
    reg.n_tiles = m["n_tiles"]
    reg.n_rows = m["n_rows"]
    reg.n_cols = m["n_cols"]
    reg.bk_dxdy_tiles = [None] * reg.n_tiles
    reg.fwd_dxdy_tiles = [None] * reg.n_tiles
    # PREP processed the images already (src_f live); REG_TILE must NOT re-process.
    reg.non_rigid_registrar_cls = _load_cls(m["non_rigid_registrar_cls"])
    reg.processing_cls = None
    reg.processing_kwargs = None
    reg.target_stats = (
        np.load(os.path.join(args.inputs_dir, "target_stats.npy"))
        if m["has_target_stats"]
        else None
    )
    reg.pbar = _NullPbar()

    reg.reg_tile(args.tile_idx, threading.Lock())  # VALIS's exact per-tile kernel

    os.makedirs(args.out_dir, exist_ok=True)
    reg.bk_dxdy_tiles[args.tile_idx].write_to_file(
        os.path.join(args.out_dir, f"bk_{args.tile_idx}.v")
    )
    reg.fwd_dxdy_tiles[args.tile_idx].write_to_file(
        os.path.join(args.out_dir, f"fwd_{args.tile_idx}.v")
    )
    logger.info(f"idx={args.tile_idx} OK (pid={os.getpid()}) -> {args.out_dir}")


if __name__ == "__main__":
    raise SystemExit(main())
