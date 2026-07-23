#!/usr/bin/env python3
"""Verify the low-memory registration path is bit-identical to what it replaces.

Leg 1 (this task): reg_finalize with the lazy pyvips reader == reg_finalize with BioFormats.
Leg 2 (Task 4):    tile fan-out + assemble == the single-process warp.

Usage (inside the VALIS image):
  docker run --rm -v "$PWD":/work -w /work \
      bolt3x/attend_image_analysis:mirage_valis_1.0.0 \
      python3 tests/integration/verify_lowmem_bitidentical.py
"""
import glob
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

# Overridable so the work dir can be put on a bind mount and survive `docker run --rm`, which is
# what makes VERIFY_LOWMEM_REUSE_PREP below useful. Default is unchanged, so CI is unaffected.
WORK = os.environ.get("VERIFY_LOWMEM_WORK", "/tmp/verify_lowmem")
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
    """Load EVERY page of an OME-TIFF as one array.

    ``n=-1`` is load-bearing. Both writers this test compares stack the C channels VERTICALLY into
    pages with ``page-height=H`` -- valis's slide_io.save_ome_tiff (arrayjoin(bandsplit(), across=1),
    slide_io.py:2905-2910) and reg_finalize._save_ome_pyvips both -- and pyvips' default ``n=1``
    reads page 0 only. Comparing with the default would therefore diff CHANNEL 0 ALONE and still
    print max|delta|=0.0 while ignoring the other channels entirely (the fixtures have 3).
    """
    from valis import warp_tools
    return warp_tools.vips2numpy(pyvips.Image.new_from_file(path, n=-1)).astype(np.float64)


def warped_canvas(ws_path, field_path):
    """(height, width) of the full-res warp's output canvas, WITHOUT decoding a pixel.

    Asks the lazy warp rather than recomputing VALIS's geometry: the canvas is NOT
    ``aligned_slide_shape_rc``, because ``warp_tools.warp_img`` crops the result to ``bbox_xywh``
    (warp_tools.py:938-943). Re-deriving that rule here would just be a second, driftable copy of
    it. pyvips knows an unevaluated image's dimensions, so this costs nothing.

    The canvas does not depend on WHICH field is passed: warp_img renders the affine into
    ``oarea=out_shape_rc`` (warp_tools.py:1040-1049), the non-rigid step is a size-preserving
    mapim, and only then does it crop to the bbox. So passing the raw per-tile field here gives
    the same dimensions as the composed one.
    """
    import reg_finalize
    ws = json.load(open(ws_path))
    dxdy = pyvips.Image.new_from_file(field_path)
    warped, _ = reg_finalize.warp_source(os.path.join(INP, MOV), ws, dxdy)
    return warped.height, warped.width


def assert_full_channel_stack(arr, path, expected_hw, label):
    """Fail unless `arr` really holds every channel of the whole warped canvas.

    Pins the precondition that makes an equality result meaningful. Both writers stack the C
    channels vertically into pages, so a `px()` regression (or a pyvips default change) would
    quietly reduce the comparison to channel 0 and keep printing max|delta|=0.0 -- the same
    assert-the-outcome-but-not-the-precondition failure that already bit the A1 probe and leg 1.
    """
    h, w = expected_hw
    c = MirageVipsSlideReader(os.path.join(INP, MOV)).metadata.n_channels
    if arr.shape[0] != c * h or arr.shape[1] != w or arr.size != c * h * w:
        raise SystemExit(
            f"FAILED: {label} ({path}) loaded {arr.shape} ({arr.size} values) but the warped "
            f"canvas and source band count imply a {c}-channel {h}x{w} stack ({c * h * w} values) "
            "-- the comparison would cover only part of the slide and could pass vacuously.")
    print(f"[verify] {label} covers the full {c}x{h}x{w} channel stack", flush=True)


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


def _empty_dir(path):
    """Remove everything INSIDE `path`, leaving `path` itself in place.

    Not ``rmtree(path)``: WORK may be a bind mount (VERIFY_LOWMEM_WORK), and removing a mount
    point fails with EBUSY.
    """
    if not os.path.isdir(path):
        return
    for name in os.listdir(path):
        p = os.path.join(path, name)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        else:
            os.remove(p)


def setup():
    md = os.path.join(PREP, MOV_STEM)
    if os.environ.get("VERIFY_LOWMEM_REUSE_PREP") == "1" and os.path.isdir(md):
        # DEVELOPMENT SHORTCUT, never the default. reg_prep takes 5-15 min in this image (it is
        # linux/amd64 under emulation on arm64 hosts), which makes iterating on the legs painful.
        # It is only sound while reg_prep.py, the reader and the fixtures are all unchanged --
        # hence opt-in, loudly announced, and off in CI.
        print(f"[verify] REUSING existing prep at {md} (VERIFY_LOWMEM_REUSE_PREP=1) -- reg_prep "
              "was NOT re-run, so this result is stale if reg_prep/the reader/the fixtures "
              "changed. Unset it for an authoritative run.", flush=True)
        return md

    _empty_dir(WORK)
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
    canvas = warped_canvas(ws, field)
    assert_full_channel_stack(a, lazy_out, canvas, "leg 1 lazy output")
    assert_full_channel_stack(b, bf_out, canvas, "leg 1 BioFormats output")
    equal = a.shape == b.shape and np.array_equal(a, b)
    d = None if a.shape != b.shape else float(np.max(np.abs(a - b)))
    print("=" * 72)
    print(f"LEG 1 lazy-reader vs BioFormats: equal={equal} max|delta|={d}")
    print("=" * 72, flush=True)
    return 0 if equal else 1


_SAMPLE_BYTES = {"uchar": 1, "char": 1, "ushort": 2, "short": 2,
                 "uint": 4, "int": 4, "float": 4, "double": 8}


def report_tile_footprint(tiles_dir, grid):
    """Report what the fan-out actually cost on disk, against the uncompressed equivalent.

    The intermediate tiles are the largest artifact this path produces -- uncompressed they are
    the whole DECOMPRESSED slide, on the low-resource machine the branch exists to serve -- so the
    saving is worth measuring rather than assuming. Reported, not asserted: the ratio is entirely
    image-dependent, and these fixtures are 128px synthetic data, so a threshold here would be
    meaningless. Bit-identity is what leg 2 asserts; this is just the price tag.
    """
    paths = sorted(glob.glob(os.path.join(tiles_dir, "tile_*")))
    on_disk = sum(os.path.getsize(p) for p in paths)
    probe = pyvips.Image.new_from_file(paths[0])
    raw = grid["width"] * grid["height"] * probe.bands * _SAMPLE_BYTES[probe.format]
    print(f"[verify] {len(paths)} tiles ({os.path.splitext(paths[0])[1]}) = {on_disk / 1024:.1f} KiB "
          f"on disk vs {raw / 1024:.1f} KiB uncompressed ({raw / on_disk:.2f}x)", flush=True)


def leg2(md):
    """Tile fan-out + assemble == the single-process warp.

    Reuses leg 1's composed inputs and diffs against leg 1's own ``lazy.ome.tiff``, so this leg
    isolates exactly one variable: whether splitting the warp into independent output tiles changes
    any pixel. Expected to be bit-identical BY CONSTRUCTION -- each tile is a .crop() of the same
    demand-driven pyvips warp -- so anything other than max|delta|=0.0 is a real defect, not
    tolerance drift.
    """
    ti = os.path.join(md, "tiler_inputs")
    ws = os.path.join(md, "warp_state.json")
    field_v = os.path.join(md, "slide_dxdy.v")
    src = os.path.join(INP, MOV)

    run([sys.executable, "bin/reg_finalize.py", "--inputs-dir", ti,
         "--field", os.path.join(md, "nr", "bk.v"), "--warp-state", ws,
         "--src-slide", src, "--out", field_v, "--emit-field-only"])
    if not os.path.exists(field_v):
        raise SystemExit(f"FAILED: --emit-field-only did not write {field_v}")

    grid_f = os.path.join(md, "grid.json")
    # 48px tiles over the ~128px canvas: 3x3 with a short right column AND a short bottom row.
    # Deliberately NOT a divisor of the canvas -- an exact division (e.g. 64) makes every tile
    # full-size and never exercises a ragged edge, which is precisely where an off-by-one in the
    # partition would show up as a seam or a shifted strip.
    run([sys.executable, "bin/reg_assemble.py", "--warp-state", ws, "--src-slide", src,
         "--field", field_v, "--write-grid", grid_f, "--tile-wh", "48"])
    grid = json.load(open(grid_f))
    if len(grid["tiles"]) < 2:
        raise SystemExit(
            f"FAILED: grid has {len(grid['tiles'])} tile(s) for a {grid['width']}x{grid['height']} "
            "canvas -- a single-tile fan-out is just the whole-image warp under another name and "
            "would prove nothing about tiling.")
    ragged = [t for t in grid["tiles"] if t["w"] < grid["tile_wh"] or t["h"] < grid["tile_wh"]]
    if not ragged:
        raise SystemExit(
            f"FAILED: every tile in the {grid['n_cols']}x{grid['n_rows']} grid is a full "
            f"{grid['tile_wh']}px tile, so this leg never exercises a short edge tile. Choose a "
            f"--tile-wh that does not divide {grid['width']}x{grid['height']} exactly.")
    print(f"[verify] grid is {grid['n_cols']}x{grid['n_rows']} over {grid['width']}x{grid['height']} "
          f"with {len(ragged)}/{len(grid['tiles'])} ragged-edge tiles", flush=True)

    # Never fan out into a directory that already holds tiles: a leftover tile_<i> from an earlier
    # grid is the right size to pass reg_assemble's per-tile check while holding pixels from a
    # different run.
    tiles_dir = os.path.join(md, "tiles_out")
    if os.path.isdir(tiles_dir):
        shutil.rmtree(tiles_dir)
    for t in grid["tiles"]:
        run([sys.executable, "bin/reg_warp_tile.py", "--warp-state", ws, "--field", field_v,
             "--src-slide", src, "--grid", grid_f, "--tile-idx", str(t["idx"]),
             "--out-dir", tiles_dir])
    report_tile_footprint(tiles_dir, grid)

    tiled_out = os.path.join(md, "tiled.ome.tiff")
    run([sys.executable, "bin/reg_assemble.py", "--warp-state", ws, "--src-slide", src,
         "--grid", grid_f, "--tiles-dir", tiles_dir, "--out", tiled_out])

    a, b = px(os.path.join(md, "lazy.ome.tiff")), px(tiled_out)
    canvas = (grid["height"], grid["width"])
    assert_full_channel_stack(a, "lazy.ome.tiff", canvas, "leg 2 single-process output")
    assert_full_channel_stack(b, tiled_out, canvas, "leg 2 tiled output")
    if float(np.max(b)) == float(np.min(b)):
        raise SystemExit(
            f"FAILED: the assembled slide is constant (all {float(np.min(b))}); two blank images "
            "compare equal no matter how badly the tiling is broken.")

    equal = a.shape == b.shape and np.array_equal(a, b)
    d = None if a.shape != b.shape else float(np.max(np.abs(a - b)))
    print("=" * 72)
    print(f"LEG 2 tile fan-out vs single warp: equal={equal} max|delta|={d} "
          f"({len(grid['tiles'])} tiles, {grid['n_cols']}x{grid['n_rows']})")
    print("=" * 72, flush=True)
    return 0 if equal else 1


def main():
    md = setup()
    rc1 = leg1(md)
    rc2 = leg2(md)
    return rc1 or rc2


if __name__ == "__main__":
    raise SystemExit(main())
