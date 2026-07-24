"""Unit tests for bin/compare_registration.py (the --reg_compare diff tool).

Runs inside mirage-valis:1.0.0 (needs pyvips); harmlessly skipped under a plain python env.

What these pin, and why (handoff section 4.4 — vacuity is endemic on this branch):

  * A comparison tool that reports 0.0 no matter what is worse than no tool, so every "they
    agree" assertion here is paired with a perturbation whose EXACT expected delta is known.
  * The C channels of a written slide are C vertically-stacked PAGES. pyvips' default n=1 reads
    CHANNEL 0 ALONE, which is what made several earlier max|delta|=0.0 results meaningless. The
    multi-channel test perturbs the LAST channel only: it fails if the tool ever regresses to
    reading page 0.
  * The streaming tile loop must give the same answer as a single-shot comparison, including on
    a ragged canvas where the right column and bottom row are short. Test geometry is deliberately
    awkward, not round.
"""

import json
import os
import subprocess
import sys
import tempfile

import numpy as np

# Needs the valis image (pyvips); skip under a plain python env (CI Python job).
try:
    import pytest

    pytest.importorskip("pyvips")
except ImportError:  # stdlib __main__ runner in-image (not under pytest)
    pass

import pyvips  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_BIN = os.path.join(_HERE, "..", "..", "bin")
sys.path.insert(0, os.path.join(_BIN, "utils"))

from vips_pages import open_multiband  # noqa: E402

SCRIPT = os.path.join(_BIN, "compare_registration.py")


def _write_pages(path, arr_hwc):
    """Write (H, W, C) as a C-page TIFF with page-height set — the layout both writers produce."""
    h, w, c = arr_hwc.shape
    arr = np.ascontiguousarray(arr_hwc)
    img = pyvips.Image.new_from_memory(
        arr.tobytes(),
        w,
        h,
        c,
        {
            np.dtype("uint8"): "uchar",
            np.dtype("uint16"): "ushort",
            np.dtype("float32"): "float",
        }[arr.dtype],
    )
    stacked = pyvips.Image.arrayjoin(img.bandsplit(), across=1)
    stacked = stacked.copy()
    stacked.set_type(pyvips.GValue.gint_type, "page-height", h)
    stacked.write_to_file(path, tile=True, tile_width=16, tile_height=16, bigtiff=True)


def _run(a, b, workdir, slide="S", extra=()):
    out_json = os.path.join(workdir, "out.json")
    out_png = os.path.join(workdir, "out.png")
    proc = subprocess.run(
        [
            sys.executable,
            SCRIPT,
            "--a",
            a,
            "--b",
            b,
            "--slide",
            slide,
            "--out-json",
            out_json,
            "--out-png",
            out_png,
            *extra,
        ],
        capture_output=True,
        text=True,
    )
    report = None
    if os.path.exists(out_json):
        with open(out_json) as fh:
            report = json.load(fh)
    return proc, report, out_png


def test_identical_slides_report_zero():
    with tempfile.TemporaryDirectory() as d:
        rng = np.random.default_rng(0)
        arr = rng.integers(0, 4096, size=(37, 53, 3), dtype=np.uint16)
        a, b = os.path.join(d, "a.tif"), os.path.join(d, "b.tif")
        _write_pages(a, arr)
        _write_pages(b, arr)

        proc, rep, png = _run(a, b, d)
        assert proc.returncode == 0, proc.stderr
        # PRECONDITION: all 3 channels were compared, not just page 0.
        assert rep["shape"] == [37, 53, 3], rep["shape"]
        assert len(rep["channels"]) == 3
        assert rep["overall"]["max_abs"] == 0.0
        assert rep["overall"]["pct_differing"] == 0.0
        assert os.path.exists(png)


def test_known_perturbation_gives_exact_metrics():
    """A single altered pixel — every reported statistic is checkable by hand."""
    with tempfile.TemporaryDirectory() as d:
        arr = np.zeros((37, 53, 1), dtype=np.uint16)
        a, b = os.path.join(d, "a.tif"), os.path.join(d, "b.tif")
        _write_pages(a, arr)
        arr2 = arr.copy()
        arr2[10, 20, 0] = 300
        _write_pages(b, arr2)

        proc, rep, _ = _run(a, b, d)
        assert proc.returncode == 0, proc.stderr
        n_px = 37 * 53
        assert rep["overall"]["max_abs"] == 300.0
        assert abs(rep["overall"]["mean_abs"] - 300.0 / n_px) < 1e-12
        assert abs(rep["overall"]["rmse"] - np.sqrt(300.0**2 / n_px)) < 1e-9
        assert abs(rep["overall"]["pct_differing"] - 100.0 / n_px) < 1e-12


def test_perturbing_the_last_channel_is_seen():
    """The n=1 trap: a tool reading only page 0 would report 0.0 here and look perfect."""
    with tempfile.TemporaryDirectory() as d:
        arr = np.zeros((37, 53, 3), dtype=np.uint16)
        a, b = os.path.join(d, "a.tif"), os.path.join(d, "b.tif")
        _write_pages(a, arr)
        arr2 = arr.copy()
        arr2[5, 7, 2] = 111  # LAST channel only
        _write_pages(b, arr2)

        # Guard the guard: confirm the fixture really is page-stacked with 3 bands.
        assert open_multiband(a).bands == 3

        proc, rep, _ = _run(a, b, d)
        assert proc.returncode == 0, proc.stderr
        assert rep["overall"]["max_abs"] == 111.0
        assert rep["channels"][0]["max_abs"] == 0.0
        assert rep["channels"][1]["max_abs"] == 0.0
        assert rep["channels"][2]["max_abs"] == 111.0


def test_tiling_does_not_change_the_answer():
    """Ragged geometry: 53x37 under a 16 px tile leaves a short right column and bottom row."""
    with tempfile.TemporaryDirectory() as d:
        rng = np.random.default_rng(7)
        arr = rng.integers(0, 500, size=(37, 53, 2), dtype=np.uint16)
        arr2 = arr.copy()
        arr2[36, 52, 1] = 65535  # the last pixel of the last (short) tile
        arr2[0, 0, 0] = 9
        a, b = os.path.join(d, "a.tif"), os.path.join(d, "b.tif")
        _write_pages(a, arr)
        _write_pages(b, arr2)

        _, whole, _ = _run(a, b, d, extra=["--tile", "4096"])
        _, tiled, _ = _run(a, b, d, extra=["--tile", "16"])
        assert whole["overall"] == tiled["overall"], (
            whole["overall"],
            tiled["overall"],
        )
        assert whole["channels"] == tiled["channels"]
        expected = float(abs(int(arr2[36, 52, 1]) - int(arr[36, 52, 1])))
        assert whole["overall"]["max_abs"] == expected


def test_same_file_twice_is_rejected():
    """Comparing a path with itself is a perfect score that means nothing. It must fail loudly."""
    with tempfile.TemporaryDirectory() as d:
        arr = np.zeros((37, 53, 1), dtype=np.uint16)
        a = os.path.join(d, "a.tif")
        _write_pages(a, arr)

        proc, rep, _ = _run(a, a, d)
        assert proc.returncode != 0, proc.stdout
        assert "SAME file" in proc.stderr, proc.stderr
        assert rep is None, "no report should be written for a rejected comparison"


def test_shape_mismatch_fails_and_records_both_shapes():
    with tempfile.TemporaryDirectory() as d:
        a, b = os.path.join(d, "a.tif"), os.path.join(d, "b.tif")
        _write_pages(a, np.zeros((37, 53, 2), dtype=np.uint16))
        _write_pages(b, np.zeros((37, 48, 2), dtype=np.uint16))

        proc, rep, _ = _run(a, b, d)
        assert proc.returncode != 0
        assert rep["error"] == "shape mismatch"
        assert rep["a"] == [37, 53, 2]
        assert rep["b"] == [37, 48, 2]


if __name__ == "__main__":
    # stdlib runner: pytest is not installed in the VALIS image.
    failures = 0
    tests = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)
