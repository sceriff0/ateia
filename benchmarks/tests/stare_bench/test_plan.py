import numpy as np
import pytest

from benchmarks.stare_bench.plan import BLANK_FRACTIONS, build_plan

REG_TILED_TILE = 2048

CONFIG = {
    "seeds": [1, 2, 3],
    "field_families": ["random_fourier"],
    "methods": ["tiled", "valis", "ashlar"],
    "correlation_px": [333.0, 777.0],
    "amplitude_px": [12.0, 48.0],
    "size": [20480, 20480],
}


def test_sweep_has_exactly_eleven_points():
    assert len(BLANK_FRACTIONS) == 11
    np.testing.assert_allclose(BLANK_FRACTIONS, np.arange(0.0, 1.01, 0.1), atol=1e-9)


def test_the_sweep_samples_inside_the_measured_failure_band():
    # The prior [0, .25, .5, .75, 1.0] sweep stepped clean over the 0.40-0.70
    # band where tiles are accepted with a WRONG displacement.
    in_band = [f for f in BLANK_FRACTIONS if 0.40 <= f <= 0.70]
    assert len(in_band) >= 4, f"only {in_band} inside the failure band"


def test_every_point_is_in_the_twenty_one_point_grid():
    # Keeps the results directly comparable with
    # tests/test_tile_residual_confidence.py's denser sweep.
    grid = np.round(np.arange(0.0, 1.001, 0.05), 4)
    for f in BLANK_FRACTIONS:
        assert np.isclose(grid, round(f, 4)).any()


def test_correlation_lengths_never_align_with_the_control_grid():
    for corr in CONFIG["correlation_px"]:
        assert corr % REG_TILED_TILE != 0
        assert REG_TILED_TILE % corr != 0


def test_plan_is_the_full_cross_product():
    plan = build_plan(CONFIG)
    assert len(plan) == 3 * 1 * 3 * 2 * 2 * len(BLANK_FRACTIONS)


def test_every_record_carries_amplitude_px():
    plan = build_plan(CONFIG)
    seen = {r["amplitude_px"] for r in plan}
    assert seen == {12.0, 48.0}


def test_run_ids_are_unique_and_stable():
    a = [r["run_id"] for r in build_plan(CONFIG)]
    b = [r["run_id"] for r in build_plan(CONFIG)]
    assert a == b
    assert len(set(a)) == len(a)


def test_pairs_are_shared_across_methods():
    # Every method must see the SAME pixels, or the comparison is meaningless.
    plan = build_plan(CONFIG)
    by_pair = {}
    for r in plan:
        by_pair.setdefault(r["pair_id"], set()).add(r["method"])
    assert all(m == {"tiled", "valis", "ashlar"} for m in by_pair.values())


def test_pair_ids_are_unique_across_amplitude():
    # Two records differing only in amplitude_px must not collide into one
    # pair_id -- that would silently make two different conditions share
    # generated images.
    plan = build_plan(CONFIG)
    by_pair = {}
    for r in plan:
        key = (r["seed"], r["field_family"], r["correlation_px"],
               r["blank_fraction"])
        by_pair.setdefault(key, set()).add(r["pair_id"])
    assert all(len(v) == len(CONFIG["amplitude_px"]) for v in by_pair.values())


def test_rejects_a_correlation_length_on_the_control_grid():
    bad = dict(CONFIG, correlation_px=[float(REG_TILED_TILE)])
    with pytest.raises(ValueError, match="control grid"):
        build_plan(bad)


def test_amplitudes_sharing_an_integer_floor_get_distinct_pair_ids():
    # A bare int() truncation would collapse 12.0 and 12.5 to the same "012"
    # tag, silently pairing two different conditions to one generated image.
    cfg = dict(CONFIG, seeds=[1], correlation_px=[333.0],
               amplitude_px=[12.0, 12.5])
    plan = build_plan(cfg)
    ids = {r["pair_id"] for r in plan}
    assert len(ids) == len(BLANK_FRACTIONS) * 2


def test_correlations_sharing_an_integer_floor_get_distinct_pair_ids():
    # Same truncation hazard on the correlation_px segment, inherited from
    # the original brief: 333.0 and 333.5 must not collapse to "c333".
    cfg = dict(CONFIG, seeds=[1], correlation_px=[333.0, 333.5],
               amplitude_px=[12.0])
    plan = build_plan(cfg)
    ids = {r["pair_id"] for r in plan}
    assert len(ids) == len(BLANK_FRACTIONS) * 2
