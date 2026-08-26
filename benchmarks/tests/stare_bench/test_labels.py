import numpy as np

from benchmarks.stare_bench.fields import make_field
from benchmarks.stare_bench.labels import TAU, tile_labels


def _field():
    return make_field("affine", (256, 256), seed=1, rotation_deg=0.0, scale=1.0,
                      translation=(0.0, 0.0))


def test_fully_retained_tiles_are_registrable():
    retained = np.ones((256, 256), dtype=np.float32)
    recs = tile_labels(retained, (256, 256), tile=64, field=_field())
    assert len(recs) == 16
    assert all(r["registrable"] for r in recs)


def test_fully_blanked_tiles_are_not_registrable():
    retained = np.zeros((256, 256), dtype=np.float32)
    recs = tile_labels(retained, (256, 256), tile=64, field=_field())
    assert not any(r["registrable"] for r in recs)


def test_the_threshold_sits_where_tau_says():
    just_below = np.full((256, 256), TAU - 0.05, dtype=np.float32)
    just_above = np.full((256, 256), TAU + 0.05, dtype=np.float32)
    lo = tile_labels(just_below, (256, 256), tile=64, field=_field())
    hi = tile_labels(just_above, (256, 256), tile=64, field=_field())
    assert not any(r["registrable"] for r in lo)
    assert all(r["registrable"] for r in hi)


def test_labels_never_consult_a_registration_result():
    # The signature is the guarantee: there is nowhere to pass one.
    import inspect

    sig = inspect.signature(tile_labels)
    forbidden = {"manifest", "controls", "result", "accepted", "error"}
    assert not (set(sig.parameters) & forbidden)


def test_tau_is_consistent_with_the_measured_foreground_separator():
    # bin/tiled_reg_tile.py's docstring records mov_fg/ref_fg >= 0.4165 as the
    # measured separator. TAU is chosen on the GENERATOR side, then checked for
    # consistency here -- never fitted to that number.
    assert 0.2 <= TAU <= 0.5
