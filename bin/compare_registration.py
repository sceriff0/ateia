#!/usr/bin/env python3
"""Compare two registered slides tile-by-tile and report how far apart they are.

Used by ``--reg_compare`` to answer "is the low-memory path close enough to classic VALIS that I
keep the VALIS guarantees?" on REAL data, instead of on the 128 px synthetic fixtures the
bit-identity suite uses. Streams both images in tiles so it runs in bounded RAM on the same
low-resource machine the new path targets: peak footprint is O(TILE^2 * C), not O(slide).

Preconditions are asserted, not assumed -- every vacuous result on this branch came from checking
an outcome without pinning what makes it meaningful:

  * the two inputs must be DIFFERENT files -- comparing a slide with itself reports a perfect
    max|delta|=0.0 that says nothing about either registration path;
  * both are opened through ``vips_pages.open_multiband``, so all C channels are compared. Reading
    a written slide with pyvips' default ``n=1`` compares CHANNEL 0 ALONE and still prints 0.0;
  * a shape mismatch is a hard failure, not a 0-pixel comparison.

Dependencies are pyvips + numpy only, deliberately: ``valis`` is not imported, so this cannot fail
for a reason belonging to the registration stack it is measuring.
"""

import argparse
import json
import logging
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "utils"))

from logger import configure_logging, get_logger  # noqa: E402
from vips_pages import open_multiband  # noqa: E402

logger = get_logger(__name__)

TILE = 2048

# pyvips band format -> numpy dtype. Kept local (valis' VIPS_FORMAT_NUMPY_DTYPE is the same map)
# so this script stays valis-free.
_VIPS_TO_NUMPY = {
    "uchar": np.uint8,
    "char": np.int8,
    "ushort": np.uint16,
    "short": np.int16,
    "uint": np.uint32,
    "int": np.int32,
    "float": np.float32,
    "double": np.float64,
}


def _to_f64(region, h, w, c):
    """Decode one crop into an (h, w, c) float64 array. This is the only place pixels are read."""
    dtype = _VIPS_TO_NUMPY.get(region.format)
    if dtype is None:
        raise SystemExit(
            f"[compare_registration] unsupported band format: {region.format}"
        )
    arr = np.frombuffer(region.write_to_memory(), dtype=dtype)
    return arr.reshape(h, w, c).astype(np.float64)


def _channel_names(path, n):
    """Channel names from the OME-XML, padded with C0..Cn-1 when absent or short."""
    try:
        import re

        import tifffile

        with tifffile.TiffFile(path) as tf:
            xml = tf.ome_metadata or ""
        names = re.findall(r'<Channel[^>]*\bName="([^"]*)"', xml)
    except Exception:
        names = []
    return (list(names) + [f"C{i}" for i in range(len(names), n)])[:n]


def _write(path, payload):
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=2)


def main():
    """CLI entry point: compare two registered slides tile-by-tile and report their divergence."""
    configure_logging(level=logging.INFO)
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--a", required=True, help="classic VALIS registered slide (the reference)"
    )
    ap.add_argument(
        "--b", required=True, help="new-path registered slide (the candidate)"
    )
    ap.add_argument("--slide", required=True, help="slide id, echoed into the report")
    ap.add_argument("--out-json", required=True)
    ap.add_argument("--out-png", required=True)
    ap.add_argument("--png-max-dim", type=int, default=2048)
    ap.add_argument("--tile", type=int, default=TILE, help="streaming tile size (px)")
    args = ap.parse_args()

    # PRECONDITION: distinct files. A wiring mistake that staged the same slide twice would
    # otherwise report a flawless agreement between a path and itself.
    if os.path.realpath(args.a) == os.path.realpath(args.b):
        raise SystemExit(
            f"[compare_registration] --a and --b resolve to the SAME file ({args.a}); "
            "there is nothing to compare"
        )

    a, b = open_multiband(args.a), open_multiband(args.b)

    if (a.width, a.height, a.bands) != (b.width, b.height, b.bands):
        _write(
            args.out_json,
            {
                "slide": args.slide,
                "error": "shape mismatch",
                "a": [a.height, a.width, a.bands],
                "b": [b.height, b.width, b.bands],
            },
        )
        raise SystemExit(
            f"[compare_registration] shape mismatch: {a.width}x{a.height}x{a.bands} vs "
            f"{b.width}x{b.height}x{b.bands}"
        )

    c = a.bands
    max_abs = np.zeros(c)
    sum_abs = np.zeros(c)
    sum_sq = np.zeros(c)
    n_diff = np.zeros(c, dtype=np.int64)
    n_px = 0

    tile = max(1, int(args.tile))
    for y in range(0, a.height, tile):
        h = min(tile, a.height - y)
        for x in range(0, a.width, tile):
            w = min(tile, a.width - x)
            ta = _to_f64(a.crop(x, y, w, h), h, w, c)
            tb = _to_f64(b.crop(x, y, w, h), h, w, c)
            d = np.abs(ta - tb).reshape(-1, c)
            max_abs = np.maximum(max_abs, d.max(axis=0))
            sum_abs += d.sum(axis=0)
            sum_sq += (d**2).sum(axis=0)
            n_diff += (d > 0).sum(axis=0)
            n_px += h * w

    names = _channel_names(args.a, c)
    report = {
        "slide": args.slide,
        "shape": [a.height, a.width, c],
        "a": os.path.basename(args.a),
        "b": os.path.basename(args.b),
        "channels": [
            {
                "index": i,
                "name": names[i],
                "max_abs": float(max_abs[i]),
                "mean_abs": float(sum_abs[i] / n_px),
                "rmse": float(np.sqrt(sum_sq[i] / n_px)),
                "pct_differing": float(100.0 * n_diff[i] / n_px),
            }
            for i in range(c)
        ],
        "overall": {
            "max_abs": float(max_abs.max()),
            "mean_abs": float(sum_abs.sum() / (n_px * c)),
            "rmse": float(np.sqrt(sum_sq.sum() / (n_px * c))),
            "pct_differing": float(100.0 * n_diff.sum() / (n_px * c)),
        },
    }
    _write(args.out_json, report)

    # Visualisation only. The magnitude image is built lazily and resized BEFORE the cast, so
    # libvips streams it; the uchar cast saturates at 255, which is fine for "where do they
    # differ" and is why the JSON, not the PNG, carries the numbers.
    diff = (a.cast("float") - b.cast("float")).abs()
    if diff.bands > 1:
        diff = diff.bandmean()
    scale = min(1.0, args.png_max_dim / float(max(a.width, a.height)))
    diff.resize(scale).cast("uchar").write_to_file(args.out_png)

    logger.info(
        f"{args.slide}: {a.width}x{a.height}x{c} "
        f"max|delta|={report['overall']['max_abs']} "
        f"rmse={report['overall']['rmse']:.6g} "
        f"pct_differing={report['overall']['pct_differing']:.6f}%"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
