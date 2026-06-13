"""Unit test: bin/utils/tile_io write_field/read_field must be a LOSSLESS float32 round-trip.

Per spec §5 precondition 2 + §7: per-tile displacement fields are in-memory float32 and the
on-disk container between REG_TILE and REG_STITCH must not quantize or compress lossily, or the
stitched field diverges from classic VALIS. Equality here must be EXACT (np.array_equal), not
approximate. Runs inside mirage-valis:1.0.0 (needs `valis`/pyvips).
"""
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "bin", "utils"))

from tile_io import read_field, write_field  # noqa: E402


def test_float32_roundtrip_is_lossless():
    rng = np.random.default_rng(0)
    field = rng.standard_normal((128, 96, 2)).astype(np.float32)  # (H, W, 2) dx/dy
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "f.v")
        write_field(field, p)
        out = read_field(p)
    assert out.dtype == np.float32
    assert out.shape == field.shape
    assert np.array_equal(out, field)  # exact, not approximate


def test_roundtrip_preserves_extreme_and_negative_values():
    field = np.array(
        [[[1e-7, -1e-7], [3.4e38, -3.4e38]], [[0.0, -0.0], [123.456, -987.654]]],
        dtype=np.float32,
    )
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "f.v")
        write_field(field, p)
        out = read_field(p)
    assert np.array_equal(out, field)


def test_roundtrip_matches_a_realistic_deepflow_field():
    # small smooth field resembling an optical-flow displacement
    yy, xx = np.mgrid[0:64, 0:48]
    dx = (np.sin(xx / 7.0) * 2.3).astype(np.float32)
    dy = (np.cos(yy / 5.0) * 1.1).astype(np.float32)
    field = np.dstack([dx, dy]).astype(np.float32)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "f.v")
        write_field(field, p)
        out = read_field(p)
    assert np.array_equal(out, field)


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(1 if failed else 0)
