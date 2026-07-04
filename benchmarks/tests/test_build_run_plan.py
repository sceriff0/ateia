from pathlib import Path

import pytest

from benchmarks.build_run_plan import build_run_plan

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


def test_registration_param_grid_crosses_params_in_both_paths():
    sweep = {
        "strategy": "ofat",
        "baseline": {"target_px": 4096, "n_channels": 2, "n_register_images": 2,
                     "memory_mode": "medium", "skip_micro_registration": True,
                     "reg_distributed_tiling": False},
        "registration_param_grid": {
            "memory_mode": ["low", "medium", "high"],
            "skip_micro_registration": [True, False],
        },
        "axes": {},
    }
    plan = build_run_plan(sweep, repeats=1)
    rp = [r for r in plan if r["varied_axis"] == "registration_param_grid"]
    # 3 memory_mode x 2 skip_micro x 2 paths (classic + distributed)
    assert len(rp) == 12
    classic = [r for r in rp if r["reg_distributed_tiling"] is False]
    dist = [r for r in rp if r["reg_distributed_tiling"] is True]
    assert len(classic) == 6 and len(dist) == 6
    # every distributed one is forced onto the SEPARATED path
    assert all(r["reg_dist_sub_threshold"] == "force" and r["reg_dist_force_tiling"] is False for r in dist)
    # both paths cover the same (memory_mode, skip_micro) combinations
    combo = lambda rs: {(r["memory_mode"], r["skip_micro_registration"]) for r in rs}
    assert combo(classic) == combo(dist)
    assert len(combo(classic)) == 6


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


def test_distributed_tiling_grid_pins_force_tiling_and_crosses_knobs():
    sweep = {
        "strategy": "ofat",
        "baseline": {"target_px": 4096, "reg_distributed_tiling": False},
        "distributed_tiling_grid": {
            "reg_dist_tile_wh": [256, 512],
            "reg_dist_tile_buffer": [50, 100],
        },
    }
    plan = build_run_plan(sweep, repeats=1)
    grid = [r for r in plan if r["varied_axis"] == "distributed_tiling_grid"]
    assert len(grid) == 4  # 2 tile_wh x 2 tile_buffer
    # every grid run pins the tiled fan-out on (else the tile knobs are no-ops) AND forces the
    # distributed adapter (else 'auto' routes small cells back to classic — no tiling measured)
    assert all(r["reg_distributed_tiling"] is True and r["reg_dist_force_tiling"] is True
               and r["reg_dist_sub_threshold"] == "force" for r in grid)
    assert {(r["reg_dist_tile_wh"], r["reg_dist_tile_buffer"]) for r in grid} == {
        (256, 50), (256, 100), (512, 50), (512, 100)}


def test_project_sweep_distributed_grid_forces_distributed_across_sizes():
    import yaml
    sweep = yaml.safe_load(
        (Path(__file__).parents[1] / "configs" / "sweep.yaml").read_text())
    # distributed is measured across sizes via distributed_grid (NOT a no-op OFAT axis)
    assert "reg_distributed_tiling" not in sweep["axes"], "distributed must not be an OFAT axis (auto-routing hides it)"
    assert sweep["baseline"]["reg_distributed_tiling"] is False
    dg = sweep["distributed_grid"]
    assert dg["target_px"] == sweep["scaling_grid"]["target_px"]  # same sizes as classic scaling grid
    plan = build_run_plan(sweep, repeats=1)
    dist = [r for r in plan if r["varied_axis"] == "distributed_grid"]
    assert dist, "distributed_grid must emit runs"
    # every distributed run is FORCED (so 'auto' can't fall back to classic) on the SEPARATED path
    for r in dist:
        assert r["reg_distributed_tiling"] is True
        assert r["reg_dist_sub_threshold"] == "force"
        assert r["reg_dist_force_tiling"] is False
    # each distributed cell has a matching classic scaling_grid cell to pair against
    classic_cells = {(r["target_px"], r["n_channels"]) for r in plan
                     if r["varied_axis"] in ("scaling_grid", "baseline")}
    for r in dist:
        assert (r["target_px"], r["n_channels"]) in classic_cells


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
    assert max(sg["target_px"]) == 65536        # capped: 131072 dropped
    assert sg["n_channels"] == [2, 4]           # 1 not benchmarked, max 4
    # registration is measured ACROSS sizes, not only at baseline
    rg = sweep["registration_grid"]
    assert max(rg["target_px"]) == 65536
    assert rg["n_register_images"] == [4, 8]
    # input-scaling dimensions are owned by the grids, not double-covered as OFAT axes
    for k in ("target_px", "n_channels", "n_register_images"):
        assert k not in sweep["axes"], f"{k} should not be an OFAT axis"
