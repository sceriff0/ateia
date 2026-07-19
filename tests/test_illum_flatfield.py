import numpy as np, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "tests" / "testdata"))
from illum.grid import recover_grid
from illum.flatfield import estimate_flatfield, apply_field
from generate_synthetic_mosaic import make_synthetic_mosaic


def test_flatfield_is_mean_one():
    d = make_synthetic_mosaic(dark=0.0, n_channels=1)
    g = recover_grid(d["mosaic"], approx_tile=d["pitch"])
    ff = estimate_flatfield(d["mosaic"][0], g)
    assert abs(float(ff.mean()) - 1.0) < 1e-2
    assert ff.dtype == np.float32


def _folded_vignette_pp(img, pitch, phase):
    """Peak-to-peak of the column profile folded on the tile pitch.

    Folding averages tiles at the same within-tile offset: random per-tile
    content brightness cancels, the (position-dependent) vignette survives.
    This isolates exactly what flat-field correction targets — unlike raw
    column std, which is dominated by per-tile content amplitude.
    """
    prof = np.median(img, axis=0).astype(np.float64)
    P = int(round(pitch))
    n = (len(prof) - int(phase)) // P
    folded = np.mean([prof[phase + i * P: phase + i * P + P] for i in range(n)], axis=0)
    return float(np.ptp(folded))


def test_apply_reduces_periodic_vignette():
    # Flat-field correction must flatten the PERIODIC vignette (isolated by
    # folding on the pitch). Raw column std is dominated by per-tile content
    # brightness — real signal correction cannot remove — so it is not the
    # right quantity to assert on.
    d = make_synthetic_mosaic(dark=0.0, vignette_strength=0.5, n_channels=1)
    ch = d["mosaic"][0]
    g = recover_grid(d["mosaic"], approx_tile=d["pitch"])
    ff = estimate_flatfield(ch, g)
    corr = apply_field(ch, ff, g)
    before = _folded_vignette_pp(ch, g["pitch_x"], g["phase_x"])
    after = _folded_vignette_pp(corr, g["pitch_x"], g["phase_x"])
    assert after < 0.6 * before
