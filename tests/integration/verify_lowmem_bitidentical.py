#!/usr/bin/env python3
"""Verify the low-memory registration path is bit-identical to what it replaces.

Leg 1 (this task): reg_finalize with the lazy pyvips reader == reg_finalize with BioFormats.
Leg 2 (Task 4):    tile fan-out + assemble == the single-process warp.

Usage (inside the VALIS image):
  docker run --rm -v "$PWD":/work -w /work \
      bolt3x/attend_image_analysis:mirage_valis_1.0.0 \
      python3 tests/integration/verify_lowmem_bitidentical.py
"""
import json
import os
import re
import shutil
import subprocess
import sys

import numpy as np
import pyvips
import tifffile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin", "utils"))

from mirage_slide_reader import MirageVipsSlideReader  # noqa: E402

WORK = "/tmp/verify_lowmem"
INP = os.path.join(WORK, "in")
PREP = os.path.join(WORK, "prep")
REF, MOV = "P001_ref.ome.tiff", "P001_mov1.ome.tiff"
MOV_STEM = "P001_mov1"

# reg_finalize.py's exact startup print lines (bin/reg_finalize.py:199 / 203) -- the only
# externally-observable evidence of which reader path a run actually took.
LAZY_MARKER = "all inputs readable by MirageVipsSlideReader; skipping JVM"
BF_MARKER = "started BioFormats JVM (heap="

# bin/preprocess.py:391-399's exact OME-TIFF writer settings (tiled BigTIFF, ome, zlib), except
# for tile size: preprocess.py uses 2048px tiles sized for whole-slide input, but these fixtures
# are 128px images -- a 2048px tile would collapse to a single tile and never exercise tiling at
# all. 64px genuinely tiles a 128px image into a 2x2 grid.
_REWRITE_TILE_PX = 64


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write("\n".join((r.stdout + r.stderr).splitlines()[-15:]))
        raise SystemExit(f"FAILED: {' '.join(cmd[1:4])}")
    return r


def px(path):
    from valis import warp_tools
    return warp_tools.vips2numpy(pyvips.Image.new_from_file(path)).astype(np.float64)


def _rewrite_as_mirage_tiff(src_path, dst_path, tile_px=_REWRITE_TILE_PX):
    """Read a checked-in fixture and rewrite it using bin/preprocess.py's EXACT OME-TIFF
    writer settings (tiled, BigTIFF, ome=True, zlib -- bin/preprocess.py:391-399), preserving
    pixel values and channel names exactly.

    Why: tests/testdata/P001_*.ome.tiff are plain ``tifffile.imwrite`` output -- NOT tiled, NOT
    BigTIFF. MirageVipsSlideReader.can_read() returns False for them, so copying them directly
    (as this setup used to do) makes BOTH legs of the leg-1 comparison silently fall back to
    BioFormats: a bit-identical result would prove nothing about the lazy-reader swap this test
    exists to verify. Real pipeline input IS tiled BigTIFF, so rewrite into that shape here
    rather than touching the committed fixtures (other tests depend on those as-is).
    """
    with tifffile.TiffFile(src_path) as tf:
        arr = tf.asarray()  # (C, H, W), same dtype as the source
        ome_xml = tf.ome_metadata or ""
    n_channels = arr.shape[0] if arr.ndim == 3 else 1
    channel_names = re.findall(r'<Channel[^>]*\bName="([^"]*)"', ome_xml)
    if len(channel_names) < n_channels:
        channel_names = list(channel_names) + [f"C{i}" for i in range(len(channel_names), n_channels)]
    channel_names = channel_names[:n_channels]

    tifffile.imwrite(
        dst_path,
        arr,
        photometric="minisblack",
        metadata={
            "axes": "CYX",
            "Channel": {"Name": channel_names},
            "PhysicalSizeX": 0.325,
            "PhysicalSizeXUnit": "µm",
            "PhysicalSizeY": 0.325,
            "PhysicalSizeYUnit": "µm",
        },
        bigtiff=True,
        ome=True,
        compression="zlib",
        tile=(tile_px, tile_px),
    )


def setup():
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(INP)
    for s in (REF, MOV):
        dst = os.path.join(INP, s)
        _rewrite_as_mirage_tiff(os.path.join("tests/testdata", s), dst)
        if not MirageVipsSlideReader.can_read(dst):
            raise SystemExit(
                f"FAILED: rewritten fixture {s} is not readable by MirageVipsSlideReader "
                "(can_read() returned False) -- the tiled-BigTIFF rewrite did not produce a "
                "file the lazy reader recognises, so leg 1 would silently exercise BioFormats "
                "on both sides again. Aborting rather than let that happen quietly.")
        print(f"[verify] rewritten {s} is readable by MirageVipsSlideReader (can_read=True)", flush=True)
    run([sys.executable, "bin/reg_prep.py", "--input-dir", INP, "--out", PREP,
         "--reference", REF, "--memory-mode", "low", "--skip-micro-registration"])
    return os.path.join(PREP, MOV_STEM)


def assert_used_reader(stdout, expect_lazy, label):
    """Fail loudly unless `stdout` shows the reg_finalize.py run took the EXPECTED reader path.

    reg_finalize.py prints exactly one of two marker lines at startup (bin/reg_finalize.py:199 /
    203) depending on whether MirageVipsSlideReader.can_read() passed on --src-slide. Without
    checking this, leg 1 can silently degrade to comparing BioFormats against BioFormats (which
    is what happened before the fixtures were tiled BigTIFF) and still print equal=True."""
    has_lazy = LAZY_MARKER in stdout
    has_bf = BF_MARKER in stdout
    if has_lazy == has_bf:  # neither marker, or (shouldn't happen) both
        raise SystemExit(
            f"FAILED: {label} produced an ambiguous reader marker "
            f"(lazy-marker-present={has_lazy}, bf-marker-present={has_bf}); expected exactly one "
            f"of reg_finalize.py's two startup print lines. stdout tail:\n"
            + "\n".join(stdout.splitlines()[-20:]))
    if expect_lazy and not has_lazy:
        raise SystemExit(
            f"FAILED: {label} was expected to use MirageVipsSlideReader (no JVM) but instead used "
            "BioFormats -- the reader swap this test exists to verify never engaged, so a "
            "bit-identical result would prove nothing. stdout tail:\n"
            + "\n".join(stdout.splitlines()[-20:]))
    if not expect_lazy and not has_bf:
        raise SystemExit(
            f"FAILED: {label} was expected to use BioFormats (MIRAGE_FORCE_BIOFORMATS=1) but "
            "instead used MirageVipsSlideReader -- MIRAGE_FORCE_BIOFORMATS did not force the JVM "
            "path, so this leg would compare the lazy reader against itself. stdout tail:\n"
            + "\n".join(stdout.splitlines()[-20:]))
    marker_line = next(l for l in stdout.splitlines() if (LAZY_MARKER if expect_lazy else BF_MARKER) in l)
    print(f"[verify] {label} used the expected reader -- {marker_line.strip()!r}", flush=True)


def assert_nontrivial_warp(ws_path, field_path, eps=1e-9):
    """Guard against a vacuous leg-1 pass: if the rigid M is ~identity AND the non-rigid field
    is ~zero, the fixture exercises no real warp, and equal=True would prove nothing about
    reader-swap faithfulness. Fail loudly instead of silently passing a meaningless comparison."""
    ws = json.load(open(ws_path))
    M = np.asarray(ws["M"], dtype=np.float64)
    max_m_delta = float(np.max(np.abs(M - np.eye(3))))

    from valis import warp_tools
    field = pyvips.Image.new_from_file(field_path)
    field_arr = warp_tools.vips2numpy(field).astype(np.float64)
    max_dxdy = float(np.max(np.abs(field_arr)))

    print(f"[verify] warp is non-trivial: max|M - I|={max_m_delta} max|dxdy|={max_dxdy}", flush=True)
    if max_m_delta <= eps and max_dxdy <= eps:
        raise SystemExit(
            f"FAILED: fixture does not exercise a real warp (max|M - I|={max_m_delta}, "
            f"max|dxdy|={max_dxdy}, both <= eps={eps}); a bit-identical leg-1 result would be "
            "meaningless because both readers would be comparing trivial passthroughs.")


def leg1(md):
    """Lazy reader vs BioFormats, same field, same warp state."""
    ti = os.path.join(md, "tiler_inputs")
    ws = os.path.join(md, "warp_state.json")
    field = os.path.join(md, "nr", "bk.v")
    os.makedirs(os.path.join(md, "nr"), exist_ok=True)
    run([sys.executable, "bin/reg_nonrigid.py", "--inputs-dir", ti,
         "--out-dir", os.path.join(md, "nr")])

    assert_nontrivial_warp(ws, field)

    lazy_out = os.path.join(md, "lazy.ome.tiff")
    bf_out = os.path.join(md, "bf.ome.tiff")
    lazy_r = run([sys.executable, "bin/reg_finalize.py", "--inputs-dir", ti, "--field", field,
                  "--warp-state", ws, "--src-slide", os.path.join(INP, MOV), "--out", lazy_out])
    assert_used_reader(lazy_r.stdout, expect_lazy=True, label="lazy-reader run")

    env = dict(os.environ, MIRAGE_FORCE_BIOFORMATS="1")
    r = subprocess.run([sys.executable, "bin/reg_finalize.py", "--inputs-dir", ti, "--field", field,
                        "--warp-state", ws, "--src-slide", os.path.join(INP, MOV), "--out", bf_out],
                       capture_output=True, text=True, env=env)
    if r.returncode != 0:
        sys.stderr.write("\n".join((r.stdout + r.stderr).splitlines()[-15:]))
        raise SystemExit("FAILED: BioFormats reference run")
    assert_used_reader(r.stdout, expect_lazy=False, label="BioFormats reference run")

    a, b = px(lazy_out), px(bf_out)
    equal = a.shape == b.shape and np.array_equal(a, b)
    d = None if a.shape != b.shape else float(np.max(np.abs(a - b)))
    print("=" * 72)
    print(f"LEG 1 lazy-reader vs BioFormats: equal={equal} max|delta|={d}")
    print("=" * 72, flush=True)
    return 0 if equal else 1


def main():
    md = setup()
    return leg1(md)


if __name__ == "__main__":
    raise SystemExit(main())
