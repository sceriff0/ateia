import numpy as np
import pytest

from benchmarks.stare_bench.fields import make_field
from benchmarks.stare_bench.plan import BLANK_FRACTIONS, build_plan

CONFIG = {
    "seeds": [1, 2, 3],
    "field_families": ["random_fourier"],
    "methods": ["tiled", "valis", "ashlar", "identity"],
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


def test_committed_correlation_lengths_pass_the_derived_pitch_firewall():
    """The real firewall (`fields.make_field`'s own pitch check), not the
    discredited `corr % REG_TILED_TILE == 0` rule build_plan used to enforce.

    That rule was neither necessary nor sufficient -- see plan.py's
    `_validate_fields_are_independent` docstring for the spec's own
    counterexamples. This calls the same dry-run `build_plan` performs, so a
    committed value that quietly stopped being safe would fail here too.
    """
    for corr in CONFIG["correlation_px"]:
        make_field("random_fourier", tuple(CONFIG["size"]), seed=0,
                  correlation_px=corr, amplitude_px=1.0)


def test_plan_is_the_full_cross_product():
    plan = build_plan(CONFIG)
    # seeds x families x correlations x amplitudes x methods (incl. the
    # "identity" do-nothing baseline) x blank fractions.
    assert len(plan) == 3 * 1 * 2 * 2 * 4 * len(BLANK_FRACTIONS)


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
    assert all(m == {"tiled", "valis", "ashlar", "identity"} for m in by_pair.values())


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


def test_rejects_a_correlation_length_with_an_unsafe_derived_pitch():
    """4096.0px at this config's 20480px size yields a derived pitch of
    ~525px -- above `MAX_COARSE_PITCH_PX` (512px, a quarter of STARE's
    2048px control-grid tile) -- so it must be rejected by the real firewall.
    2048.0px itself (the OLD, discredited rule's rejection case) yields a
    perfectly safe ~259px pitch at this size and must NOT be rejected; see
    the design spec's own counterexample this replaced the divisibility rule
    over.
    """
    bad = dict(CONFIG, correlation_px=[4096.0])
    with pytest.raises(ValueError, match="control-grid spacing"):
        build_plan(bad)

    make_field("random_fourier", tuple(CONFIG["size"]), seed=0,
              correlation_px=2048.0, amplitude_px=1.0)


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
