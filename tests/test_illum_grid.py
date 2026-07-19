import numpy as np, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests" / "testdata"))
from illum.grid import recover_grid, tile_origins
from generate_synthetic_mosaic import make_synthetic_mosaic


def test_recover_grid_matches_injected_pitch():
    d = make_synthetic_mosaic(tile=128, overlap=16, n_channels=2, dark=100.0)
    g = recover_grid(d["mosaic"], approx_tile=d["pitch"])
    assert abs(g["pitch_x"] - d["pitch"]) <= 1.0
    assert abs(g["pitch_y"] - d["pitch"]) <= 1.0


def test_tile_origins_float_pitch_no_drift():
    # pitch 112.4 across 10 tiles: last origin must track the float pitch,
    # not an integer stride of 112 (which would be 4px short by tile 10)
    origins = tile_origins(phase=0, pitch=112.4, tile_size=112, extent=112.4 * 11)
    assert origins[0] == 0
    assert origins[10] == round(112.4 * 10)   # 1124, not 1120
