import importlib.util
from pathlib import Path

import pytest

from benchmarks.build_run_plan import build_run_plan

REPO_ROOT = Path(__file__).resolve().parents[2]


def _param_checker():
    """Load tests/check_param_consistency.py (not an importable package) by path.

    That module already parses nextflow.config's `params {}` block into Python values and is the
    pipeline's own source of truth for "what is the shipped default". Reusing it keeps one Groovy
    literal parser in the repo instead of a second, drifting copy here.
    """
    path = REPO_ROOT / "tests" / "check_param_consistency.py"
    spec = importlib.util.spec_from_file_location("_check_param_consistency", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_CHECKER = _param_checker()
Unparsed = _CHECKER.Unparsed


def pipeline_param_defaults() -> dict:
    """param name -> shipped default, straight from nextflow.config."""
    return _CHECKER.extract_config_defaults((REPO_ROOT / "nextflow.config").read_text())

SWEEP = {
    "strategy": "ofat",
    "baseline": {"target_px": 4096, "n_channels": 2, "memory_mode": "medium"},
    "axes": {
        "target_px": [4096, 8192],
        "n_channels": [2, 4],
        "memory_mode": ["medium", "high"],
    },
}

GRID_SWEEP = {
    "strategy": "ofat",
    "scaling_grid": {"target_px": [2048, 4096, 8192], "n_channels": [2, 4]},
    "baseline": {"target_px": 4096, "n_channels": 2, "memory_mode": "medium"},
    "axes": {"memory_mode": ["medium", "high"]},
}

GRID_SWEEP2 = {
    "strategy": "ofat",
    "scaling_grid": {"target_px": [2048, 4096], "n_channels": [2, 4]},
    "registration_grid": {"target_px": [2048, 4096],
                          "n_register_images": [2, 4, 8], "n_channels": 2},
    "baseline": {"target_px": 4096, "n_channels": 2, "n_register_images": 2,
                 "memory_mode": "medium"},
    "axes": {"memory_mode": ["medium", "high"]},
}


def test_ofat_includes_one_baseline_run():
    plan = build_run_plan(SWEEP)
    baseline_runs = [r for r in plan if r["varied_axis"] == "baseline"]
    assert len(baseline_runs) == 1
    assert baseline_runs[0]["memory_mode"] == "medium"


def test_ofat_varies_one_axis_at_a_time_off_baseline():
    plan = build_run_plan(SWEEP)
    # non-baseline values: target_px=8192, n_channels=4, memory_mode=high -> 3 runs
    varied = [r for r in plan if r["varied_axis"] != "baseline"]
    assert len(varied) == 3
    hi = [r for r in varied if r["varied_axis"] == "memory_mode"][0]
    # only memory_mode differs from baseline; other axes stay at baseline values
    assert hi["memory_mode"] == "high" and hi["target_px"] == 4096 and hi["n_channels"] == 2


def test_run_ids_are_unique():
    plan = build_run_plan(SWEEP)
    ids = [r["run_id"] for r in plan]
    assert len(ids) == len(set(ids))


def test_grid_is_full_cross_product():
    sweep = dict(SWEEP, strategy="grid")
    plan = build_run_plan(sweep)
    # 2 * 2 * 2 = 8 cells
    assert len(plan) == 8


def test_repeats_expands_each_config_with_unique_ids():
    plan = build_run_plan(SWEEP, repeats=3)
    # 4 configs (baseline + 3 OFAT axes) x 3 repeats = 12 runs
    assert len(plan) == 12
    assert len({r["run_id"] for r in plan}) == 12          # unique launch dirs
    by_cfg = {}
    for r in plan:
        by_cfg.setdefault(r["config_id"], []).append(r["rep"])
    assert len(by_cfg) == 4                                 # 4 distinct configs
    assert all(sorted(reps) == [0, 1, 2] for reps in by_cfg.values())


def test_repeats_default_one_reproduces_single_shot():
    assert len(build_run_plan(SWEEP)) == 4                  # baseline + 3 varied, no dup


def test_repeats_below_one_rejected():
    with pytest.raises(ValueError):
        build_run_plan(SWEEP, repeats=0)


def test_scaling_grid_is_full_size_channel_cross():
    plan = build_run_plan(GRID_SWEEP, repeats=1)
    grid_cells = {(r["target_px"], r["n_channels"]) for r in plan
                  if r["varied_axis"] in ("scaling_grid", "baseline")}
    assert grid_cells == {(2048, 2), (2048, 4), (4096, 2),
                          (4096, 4), (8192, 2), (8192, 4)}
    # the baseline cell (4096, 2) is emitted exactly once, labelled baseline
    base = [r for r in plan if r["varied_axis"] == "baseline"]
    assert len(base) == 1
    assert (base[0]["target_px"], base[0]["n_channels"]) == (4096, 2)
    # OFAT knobs still run, held at the baseline input cell
    mm = [r for r in plan if r["varied_axis"] == "memory_mode"]
    assert len(mm) == 1 and mm[0]["target_px"] == 4096 and mm[0]["n_channels"] == 2


def test_registration_grid_crosses_size_and_rounds():
    plan = build_run_plan(GRID_SWEEP2, repeats=1)
    reg = [(r["target_px"], r["n_register_images"]) for r in plan
           if r["varied_axis"] == "registration_grid"]
    # 2 sizes x rounds {4,8} (N=2 skipped — covered by the scaling grid) = 4 configs,
    # all at the fixed 2 channels
    assert set(reg) == {(2048, 4), (2048, 8), (4096, 4), (4096, 8)}
    assert all(r["n_channels"] == 2 for r in plan
               if r["varied_axis"] == "registration_grid")


def test_registration_grid_crosses_channels_when_list():
    sweep = {
        "strategy": "ofat",
        "scaling_grid": {"target_px": [2048, 4096], "n_channels": [2, 4]},
        "registration_grid": {"target_px": [2048, 4096],
                              "n_register_images": [4, 8], "n_channels": [2, 4]},
        "baseline": {"target_px": 4096, "n_channels": 2, "n_register_images": 2,
                     "memory_mode": "medium"},
        "axes": {"memory_mode": ["medium", "high"]},
    }
    plan = build_run_plan(sweep, repeats=1)
    reg = {(r["target_px"], r["n_channels"], r["n_register_images"]) for r in plan
           if r["varied_axis"] == "registration_grid"}
    # 2 sizes x {2,4} channels x {4,8} rounds = 8 configs (N=2 skipped, covered by scaling grid)
    assert reg == {
        (2048, 2, 4), (2048, 2, 8), (2048, 4, 4), (2048, 4, 8),
        (4096, 2, 4), (4096, 2, 8), (4096, 4, 4), (4096, 4, 8)}


def test_registration_param_grid_crosses_params_classic():
    sweep = {
        "strategy": "ofat",
        "baseline": {"target_px": 4096, "n_channels": 2, "n_register_images": 2,
                     "memory_mode": "medium", "reg_micro_reg": 0},
        "registration_param_grid": {
            "memory_mode": ["low", "medium", "high"],
            "reg_micro_reg": [0, 1, 2],
        },
        "axes": {},
    }
    plan = build_run_plan(sweep, repeats=1)
    rp = [r for r in plan if r["varied_axis"] == "registration_param_grid"]
    # 3 memory_mode x 3 reg_micro_reg depths (0/1/2), VALIS path only (reg_micro_reg replaced the old
    # boolean skip_micro_registration when the STARE merge landed on main).
    assert len(rp) == 9
    combo = {(r["memory_mode"], r["reg_micro_reg"]) for r in rp}
    assert combo == {(mm, rmr) for mm in ("low", "medium", "high") for rmr in (0, 1, 2)}
    # the distributed knobs must not appear anywhere
    assert all("reg_distributed_tiling" not in r and "reg_dist_force_tiling" not in r for r in rp)


def test_pinned_grid_pins_the_gate_and_crosses_live_knobs():
    # Params behind a feature gate must be swept with the gate ON, or the run varies a value the
    # pipeline never reads: full cost, zero signal. Synthetic params: this covers the expander
    # mechanism, which must keep working for whatever gated knob the sweep grows next.
    sweep = {
        "strategy": "ofat",
        "baseline": {"target_px": 4096, "n_channels": 2,
                     "enable_gate": False, "knob_a": "x", "knob_b": 1024},
        "pinned_grids": {
            "gated_knobs": {
                "pin": {"enable_gate": True},
                "cross": {"knob_a": ["x", "y"], "knob_b": [512, 1024]},
            },
        },
        "axes": {},
    }
    plan = build_run_plan(sweep, repeats=1)
    pinned = [r for r in plan if r["varied_axis"] == "pinned_grid:gated_knobs"]
    assert len(pinned) == 4                                    # 2 knob_a x 2 knob_b
    assert all(r["enable_gate"] is True for r in pinned)
    assert {(r["knob_a"], r["knob_b"]) for r in pinned} == {
        ("x", 512), ("x", 1024), ("y", 512), ("y", 1024)}
    # the baseline run keeps the gate OFF
    assert [r for r in plan if r["varied_axis"] == "baseline"][0]["enable_gate"] is False


def test_project_sweep_exercises_the_tiled_fanout_path():
    # At reg_tiled_fanout=false, TILED_COARSE/TILED_REG_TILE/TILED_SOLVE/TILED_STITCH never execute.
    # The sweep must produce runs on BOTH sides or those four processes are never measured at all.
    import yaml
    sweep = yaml.safe_load(
        (Path(__file__).parents[1] / "configs" / "sweep.yaml").read_text())
    plan = build_run_plan(sweep, repeats=1)
    tiled = [r for r in plan if r.get("registration_method") == "tiled"]
    assert tiled, "no tiled/STARE runs in the plan"
    assert {r["reg_tiled_fanout"] for r in tiled} == {False, True}


def test_project_sweep_covers_every_shipped_segmentation_backend():
    import yaml
    sweep = yaml.safe_load(
        (Path(__file__).parents[1] / "configs" / "sweep.yaml").read_text())
    # nextflow.config's seg_method enum -- all three must be benchmarked somewhere in the plan.
    plan = build_run_plan(sweep, repeats=1)
    assert {r["seg_method"] for r in plan} == {"stardist", "instantseg", "cellsam"}


def test_project_sweep_has_no_dead_axes():
    # An OFAT axis on a param that is gated behind another param is a no-op run. These belong in
    # pinned_grids (gate pinned on) or a per-method grid, never in flat `axes`.
    import yaml
    sweep = yaml.safe_load(
        (Path(__file__).parents[1] / "configs" / "sweep.yaml").read_text())
    gated = {
        "reg_tiled_tile", "reg_tiled_gate_tre", "reg_tiled_fanout",   # registration_method=tiled
        "reg_tiled_halo", "reg_tiled_upsample", "reg_tiled_out_tile",
        "seg_n_tiles_x", "seg_n_tiles_y",                              # seg_method=stardist
        "seg_instantseg_tile_size", "seg_instantseg_batch_size",       # seg_method=instantseg
        "seg_cellsam_block_size", "seg_cellsam_bbox_threshold",        # seg_method=cellsam
    }
    dead = gated & set(sweep["axes"])
    assert not dead, f"gated params used as flat OFAT axes (they would measure nothing): {sorted(dead)}"


def test_segmentation_grid_pins_method_and_crosses_own_params():
    sweep = {
        "strategy": "ofat",
        "baseline": {"target_px": 4096, "n_channels": 2, "seg_method": "stardist",
                     "seg_n_tiles_x": 16, "seg_n_tiles_y": 16},
        "segmentation_grid": {
            "stardist": {"seg_n_tiles_x": [8, 16, 32], "seg_n_tiles_y": [8, 16, 32]},
            "cellsam": {"seg_cellsam_block_size": [256, 400, 512]},
            "instantseg": {"seg_instantseg_tile_size": [256, 512, 1024]},
        },
        "axes": {},
    }
    plan = build_run_plan(sweep, repeats=1)
    sd = [r for r in plan if r["varied_axis"] == "segmentation_grid:stardist"]
    cs = [r for r in plan if r["varied_axis"] == "segmentation_grid:cellsam"]
    ins = [r for r in plan if r["varied_axis"] == "segmentation_grid:instantseg"]
    assert len(sd) == 9 and len(cs) == 3 and len(ins) == 3       # 3x3, 3, 3
    assert all(r["seg_method"] == "stardist" for r in sd)
    assert all(r["seg_method"] == "cellsam" for r in cs)
    assert all(r["seg_method"] == "instantseg" for r in ins)
    assert {(r["seg_n_tiles_x"], r["seg_n_tiles_y"]) for r in sd} == {
        (x, y) for x in (8, 16, 32) for y in (8, 16, 32)}
    assert {r["seg_cellsam_block_size"] for r in cs} == {256, 400, 512}


def test_project_sweep_has_no_distributed_surface():
    # the distributed/tiled registration path was archived out of the pipeline; the sweep must not
    # reference any of its params or grids (they would pass dead --param flags to the pipeline).
    import yaml
    sweep = yaml.safe_load(
        (Path(__file__).parents[1] / "configs" / "sweep.yaml").read_text())
    assert "distributed_grid" not in sweep
    assert "distributed_tiling_grid" not in sweep
    plan = build_run_plan(sweep, repeats=1)
    for r in plan:
        for k in r:
            assert not k.startswith("reg_dist"), f"distributed param {k} leaked into the run plan"
        assert "reg_distributed_tiling" not in r


def test_project_sweep_enables_qc_signals():
    # the paper's accuracy + segmentation-quality tables are harvested from pipeline QC, so the
    # baseline must turn those QC steps on.
    import yaml
    sweep = yaml.safe_load(
        (Path(__file__).parents[1] / "configs" / "sweep.yaml").read_text())
    assert sweep["baseline"]["reg_qc"] == 2                       # staged registration QC (dice + displacement)
    assert sweep["baseline"]["skip_seg_quality_eval"] is False     # CellSegmentationEvaluator on


def test_project_sweep_baseline_matches_pipeline_defaults():
    """Every baseline value must equal the shipped nextflow.config default.

    Read straight from nextflow.config rather than mirrored as literals here. The previous
    hand-maintained assertion list said "update BOTH sides together if a pipeline default changes",
    and that is exactly what failed: main flipped seg_method from 'stardist' to 'instantseg' and the
    sweep kept claiming stardist was the pipeline default, because seg_method was never asserted.
    Deriving the expected values means a new pipeline default cannot silently desync the baseline --
    the same approach tests/check_param_consistency.py takes for the schema.
    """
    import yaml
    base = yaml.safe_load(
        (Path(__file__).parents[1] / "configs" / "sweep.yaml").read_text())["baseline"]
    config_defaults = pipeline_param_defaults()

    # Synthetic matrix dimensions: owned by generate_matrix.py, not pipeline params.
    synthetic = {"target_px", "n_channels", "n_register_images"}

    checked = 0
    for name, baseline_value in base.items():
        if name in synthetic:
            continue
        assert name in config_defaults, (
            f"sweep baseline sets '{name}', which is not a pipeline param in nextflow.config"
        )
        expected = config_defaults[name]
        if isinstance(expected, Unparsed):
            continue  # Groovy literal the parser cannot evaluate; nothing to compare
        assert baseline_value == expected and (
            isinstance(baseline_value, bool) == isinstance(expected, bool)
        ), (
            f"sweep baseline '{name}' = {baseline_value!r} but nextflow.config default is "
            f"{expected!r}. The baseline run must BE the shipped config -- update sweep.yaml "
            f"(or, if the pipeline default moved deliberately, update both together)."
        )
        checked += 1

    assert checked > 20, f"only {checked} baseline params checked -- parser likely broke"


def test_project_sweep_all_configs_share_columns():
    # run_sweep.sh maps EVERY csv column to --param, so a config missing a key another config has would
    # write a blank cell -> `--param ""` for those runs. Guard: all configs carry the same key set.
    import yaml
    sweep = yaml.safe_load(
        (Path(__file__).parents[1] / "configs" / "sweep.yaml").read_text())
    plan = build_run_plan(sweep, repeats=1)
    keysets = {frozenset(r) for r in plan}
    assert len(keysets) == 1, "configs have differing columns (baseline must declare every swept param)"


def test_project_sweep_caps_and_grids():
    import yaml
    sweep = yaml.safe_load(
        (Path(__file__).parents[1] / "configs" / "sweep.yaml").read_text())
    sg = sweep["scaling_grid"]
    assert max(sg["target_px"]) == 90000        # largest benchmarked size (see sweep.yaml scaling_grid)
    assert sg["n_channels"] == [2, 4]           # 1 not benchmarked, max 4
    # registration is measured ACROSS sizes, not only at baseline
    rg = sweep["registration_grid"]
    assert max(rg["target_px"]) == 90000
    assert rg["n_register_images"] == [4, 8]
    # input-scaling dimensions are owned by the grids, not double-covered as OFAT axes
    for k in ("target_px", "n_channels", "n_register_images"):
        assert k not in sweep["axes"], f"{k} should not be an OFAT axis"
