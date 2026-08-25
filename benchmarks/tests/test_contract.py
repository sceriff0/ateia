"""What identifies a run is stated once, in sweep.yaml.

It used to be stated three times:

  * correctly in sweep.yaml, which is where the sweep is actually defined;
  * as DEAD documentation in make_tables.CONFIG_COLS -- 33 entries described as
    the run's identity, imported by nothing (the only occurrence in the tree was
    its own definition);
  * as a hardcoded 14-entry carry in quality.py::run_cost_summary, which is the
    one that actually ran, and which had already drifted.

Swept axes were therefore absent from the tables, so two runs differing only on
them produced indistinguishable rows -- including reg_max_image_dim, which
sweep.yaml itself calls the dominant runtime-and-accuracy axis of VALIS and
sweeps over [2000, 4000, 8000].

Adding an axis to sweep.yaml must now carry it into the tables with no other
edit. That property is what these tests pin.
"""
from pathlib import Path

from benchmarks.analysis.lib import contract

BENCH = Path(__file__).resolve().parents[1]
SWEEP = BENCH / "configs" / "sweep.yaml"


def test_every_swept_axis_is_an_identity_column():
    sweep = contract.load_sweep(SWEEP)
    axes = contract.axes(sweep)
    assert axes, "no axes found in sweep.yaml -- the parser is wrong, not the file"
    identity = set(contract.identity_columns(sweep))
    missing = axes - identity
    assert not missing, (
        f"swept but not carried into the tables, so runs differing only on these "
        f"collapse into indistinguishable rows: {sorted(missing)}"
    )


def test_the_known_regression_axis_is_present():
    """reg_max_image_dim was swept over [2000, 4000, 8000] and absent from the
    carry. Named explicitly so a future refactor cannot silently drop the one
    axis that proved the defect."""
    sweep = contract.load_sweep(SWEEP)
    assert "reg_max_image_dim" in contract.identity_columns(sweep)


def test_a_baseline_only_param_is_not_an_axis():
    """DECLARED is not SWEPT. cleanup_level is pinned in the baseline and varied
    nowhere, so it must NOT become an identity column -- every run shares its
    value, and a constant column identifies nothing while widening every table."""
    sweep = contract.load_sweep(SWEEP)
    assert "cleanup_level" in sweep["baseline"]
    assert "cleanup_level" not in contract.axes(sweep)


def test_a_single_valued_axis_is_not_an_axis_either():
    """An `axes:` entry with one value produces one run, at that value. It is a
    pin written in the axes block, not something the sweep varies."""
    sweep = {
        "strategy": "ofat",
        "axes": {"pinned": ["only"], "real": ["a", "b"]},
        "baseline": {"pinned": "only", "real": "a"},
    }
    assert contract.axes(sweep) == {"real"}


def test_grid_keys_count_as_axes():
    """A *_grid varies its keys by construction, including the nested
    per-backend form segmentation_grid uses.

    `seg_method` is in the expected set and appears NOWHERE in the grid: the
    variant names ARE its values, and build_run_plan pins it per block. That is
    the case a YAML-shape walker cannot see without restating build_run_plan's
    mapping, and the reason axes() expands the plan instead.
    """
    sweep = {
        "strategy": "ofat",
        "axes": {},
        "baseline": {"target_px": 1, "n_channels": 2},
        "scaling_grid": {"target_px": [1, 2], "n_channels": [2, 4]},
        "segmentation_grid": {
            "stardist": {"seg_n_tiles_x": [8, 16]},
            "cellsam": {"seg_cellsam_block_size": [256, 512]},
        },
    }
    assert contract.axes(sweep) == {
        "target_px", "n_channels", "seg_method",
        "seg_n_tiles_x", "seg_cellsam_block_size",
    }


def test_identity_is_deterministic_and_carries_the_structural_columns():
    sweep = contract.load_sweep(SWEEP)
    cols = contract.identity_columns(sweep)
    assert cols == contract.identity_columns(sweep), "identity_columns is not stable"
    for structural in ("run_id", "config_id", "rep", "varied_axis"):
        assert structural in cols
    assert len(cols) == len(set(cols)), "identity_columns repeats a column"


def test_config_cols_is_gone():
    """A 33-entry list documented as the run's identity, imported by nothing.
    Dead documentation is worse than none: it looks authoritative, so the next
    reader maintains it instead of the list that runs."""
    src = (BENCH / "analysis" / "make_tables.py").read_text()
    # A DEFINITION, not any mention: the comment left in its place explains why
    # the constant is gone, and a check that could not tell those apart would
    # have to be deleted the first time someone documented the removal.
    import re
    assert not re.search(r"^\s*CONFIG_COLS\s*=", src, re.M), (
        "CONFIG_COLS is defined again in make_tables.py"
    )


def test_the_cost_carry_is_derived_not_hardcoded():
    src = (BENCH / "analysis" / "lib" / "quality.py").read_text()
    assert "identity_columns" in src, (
        "quality.py no longer derives its carry from the contract"
    )


def test_the_arms_experiment_contributes_its_own_axes():
    """quality.run_cost_summary serves BOTH experiments -- `arm-tables` runs
    make_tables against the arms results root -- so the identity is the union.

    Derived from sweep.yaml alone it loses every arms-only axis. That is not
    hypothetical: reg_ashlar_tile is swept by arms.yaml and (until Task 7.5) by
    no sweep grid, and the hardcoded carry it replaced listed it by hand with a
    comment saying that without it "two ashlar arms that differ only by tile
    size collapse into indistinguishable rows".
    """
    sweep = contract.load_sweep(SWEEP)
    arms = contract.load_sweep(BENCH / "configs" / "arms.yaml")
    arm_only = contract.arm_axes(arms) - contract.axes(sweep)
    assert arm_only, (
        "the arms experiment varies nothing the sweep does not -- either the "
        "arm plan builder changed shape or this union is now pointless"
    )
    union = set(contract.identity_columns(sweep, arms))
    assert arm_only <= union, f"arms-only axes dropped: {sorted(arm_only - union)}"


def test_the_identity_is_wider_than_the_list_it_replaced():
    """The defect in one number. The hardcoded carry was 14 columns against a
    sweep with 30+ axes; if the derived identity is not substantially wider,
    the parser is finding almost nothing and the guards above pass vacuously."""
    cols = contract.identity_columns(contract.load_sweep(SWEEP))
    assert len(cols) > 20, f"derived identity is only {len(cols)} columns: {cols}"
