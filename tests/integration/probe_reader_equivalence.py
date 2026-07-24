#!/usr/bin/env python3
"""Probe spec assumptions A1 and A2 before implementing the lazy reader.

A1: pyvips and BioFormats decode mirage's preprocessed OME-TIFF to IDENTICAL pixels.
A2: Valis.register(reader_cls=...) keeps the BioFormats JVM out of the rigid stage.

Usage (inside the VALIS image):
  docker run --rm -v "$PWD":/work -w /work \
      bolt3x/attend_image_analysis:mirage_valis_1.0.0 \
      python3 tests/integration/probe_reader_equivalence.py
"""

import os

import numpy as np
import pyvips
import tifffile

WORK = "/tmp/probe_reader"
SRC = os.path.join(WORK, "probe.ome.tiff")
H, W, C = 512, 640, 3


def make_fixture():
    """Write a fixture with mirage's EXACT writer settings (bin/preprocess.py:391-399)."""
    os.makedirs(WORK, exist_ok=True)
    rng = np.random.default_rng(0)
    arr = rng.integers(0, 65535, size=(C, H, W), dtype=np.uint16)
    tifffile.imwrite(
        SRC,
        arr,
        photometric="minisblack",
        metadata={"axes": "CYX"},
        bigtiff=True,
        ome=True,
        compression="zlib",
        tile=(
            256,
            256,
        ),  # Intentional: fixture is 512x640, so production's (2048, 2048) would yield single tile, not exercising multi-tile decoding
    )
    return arr


def read_pyvips():
    """Lazy multi-page read: toilet-roll -> per-band crop -> bandjoin."""
    img = pyvips.Image.new_from_file(SRC, access="random", n=-1)
    ph = img.get("page-height")
    bands = [img.crop(0, i * ph, img.width, ph) for i in range(img.height // ph)]
    joined = bands[0] if len(bands) == 1 else bands[0].bandjoin(bands[1:])
    mem = np.frombuffer(joined.write_to_memory(), dtype=np.uint16)
    return mem.reshape(joined.height, joined.width, joined.bands)


def read_bioformats():
    from valis import slide_io

    reader_cls = slide_io.get_slide_reader(SRC, series=0)
    reader = reader_cls(SRC, series=0)
    vips_img = reader.slide2vips(level=0)
    mem = np.frombuffer(vips_img.write_to_memory(), dtype=np.uint16)
    return mem.reshape(
        vips_img.height, vips_img.width, vips_img.bands
    ), reader_cls.__name__


def main():
    truth = make_fixture()
    truth_hwc = np.moveaxis(truth, 0, -1)

    pv = read_pyvips()
    print(f"[A1] pyvips shape={pv.shape} vs tifffile {truth_hwc.shape}", flush=True)
    a1_vs_truth = np.array_equal(pv, truth_hwc)
    print(f"[A1] pyvips == tifffile source array: {a1_vs_truth}", flush=True)

    from valis import registration

    registration.init_jvm(mem_gb=8)
    bf, reader_name = read_bioformats()
    print(f"[A1] BioFormats reader class chosen: {reader_name}", flush=True)
    a1_reader = reader_name == "BioFormatsSlideReader"
    if not a1_reader:
        print(
            f"[A1] UNEXPECTED reader {reader_name!r}: the probe is no longer exercising "
            f"BioFormats, so a pixel match proves nothing",
            flush=True,
        )
    a1_bf = bf.shape == pv.shape and np.array_equal(bf, pv)
    d = (
        None
        if bf.shape != pv.shape
        else float(np.max(np.abs(bf.astype(np.int64) - pv.astype(np.int64))))
    )
    print(f"[A1] pyvips == BioFormats: {a1_bf}  max|delta|={d}", flush=True)
    registration.kill_jvm()

    print("=" * 72)
    print(f"A1 VERDICT: {'PASS' if (a1_vs_truth and a1_bf and a1_reader) else 'FAIL'}")
    print("=" * 72, flush=True)
    return 0 if (a1_vs_truth and a1_bf and a1_reader) else 1


if __name__ == "__main__":
    raise SystemExit(main())
