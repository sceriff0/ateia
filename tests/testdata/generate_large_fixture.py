#!/usr/bin/env python3
"""Generate a larger synthetic multichannel fixture for distributed-tiling integration tests.

The 128px fixture is too small for meaningful non-rigid tiling and for micro-registration
(micro_reg_size = floor(min_max * 0.125) is degenerate). This makes a deterministic NxN
multichannel OME-TIFF pair (reference + moving with a smooth local deformation) so that:
  * non-rigid runs on a real multi-tile grid (whole-vs-tiled comparison is non-degenerate), and
  * micro-registration size is non-degenerate.

Usage:
  python3 tests/testdata/generate_large_fixture.py --size 1024 --out /tmp/bigdata
"""

import argparse
import os

import numpy as np
import tifffile


def draw(size, channel_names, seed, warp=None):
    rng = np.random.RandomState(seed)
    chans = []
    yy, xx = np.ogrid[:size, :size]
    for ch in range(len(channel_names)):
        img = np.zeros((size, size), dtype=np.float64)
        n_cells = rng.randint(int(size * 0.4), int(size * 0.8))
        for _ in range(n_cells):
            cy = rng.randint(15, size - 15)
            cx = rng.randint(15, size - 15)
            radius = rng.randint(4, 14)
            intensity = (
                rng.randint(8000, 15000) if ch == 0 else rng.randint(2000, 10000)
            )
            dist2 = (yy - cy) ** 2 + (xx - cx) ** 2
            img = np.maximum(
                img, np.exp(-dist2 / (2 * (radius / 2.0) ** 2)) * intensity
            )
        img += rng.normal(100, 20, (size, size))
        img = np.clip(img, 0, 65535)
        if warp is not None:
            # smooth local deformation so the non-rigid pass has real work (not pure translation)
            img = _smooth_warp(img, size, *warp)
        chans.append(img.astype(np.uint16))
    return np.stack(chans, axis=0)


def _smooth_warp(img, size, amp, freq):
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float64)
    dx = amp * np.sin(2 * np.pi * freq * yy / size)
    dy = amp * np.cos(2 * np.pi * freq * xx / size)
    sx = np.clip(xx + dx, 0, size - 1).astype(np.int32)
    sy = np.clip(yy + dy, 0, size - 1).astype(np.int32)
    return img[sy, sx]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=1024)
    ap.add_argument("--out", default="/tmp/bigdata")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    specs = [
        ("P001_ref.ome.tiff", ["DAPI", "PANCK", "SMA"], 1, None),
        (
            "P001_mov1.ome.tiff",
            ["DAPI", "CD3", "CD8"],
            1,
            (6.0, 2.0),
        ),  # same seed=1 sharing DAPI struct, + warp
    ]
    for fname, chans, seed, warp in specs:
        arr = draw(args.size, chans, seed, warp)
        tifffile.imwrite(
            os.path.join(args.out, fname),
            arr,
            photometric="minisblack",
            metadata={"axes": "CYX", "Channel": {"Name": chans}},
        )
        print(f"  wrote {fname} shape={arr.shape} dtype={arr.dtype}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
