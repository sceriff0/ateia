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


def test_registration_param_grid_crosses_params():
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
    # 3 memory_mode x 3 reg_micro_reg depths
    assert len(rp) == 9
    combo = {(r["memory_mode"], r["reg_micro_reg"]) for r in rp}
    assert combo == {(mm, mr) for mm in ("low", "medium", "high") for mr in (0, 1, 2)}
    # the DEAD skip_micro_registration boolean (never read by the pipeline) and the archived
    # distributed knobs must not appear anywhere
    assert all("skip_micro_registration" not in r for r in rp)
    assert all("reg_distributed_tiling" not in r and "reg_dist_force_tiling" not in r for r in rp)


def test_tiled_param_grid_pins_method_and_crosses_reg_tiled_params():
    sweep = {
        "strategy": "ofat",
        "baseline": {"target_px": 4096, "n_channels": 2, "registration_method": "valis",
                     "reg_tiled_tile": 2048, "reg_tiled_gate_tre": 1.0},
        "tiled_param_grid": {
            "reg_tiled_tile": [1024, 2048, 4096],
            "reg_tiled_gate_tre": [0.5, 1.0, 2.0],
        },
        "axes": {},
    }
    plan = build_run_plan(sweep, repeats=1)
    tp = [r for r in plan if r["varied_axis"] == "tiled_param_grid"]
    # 3 tile x 3 gate = 9 configs, all PINNED to registration_method=tiled (reg_tiled_* are no-ops
    # under valis, so the grid forces the tiled backend)
    assert len(tp) == 9
    assert all(r["registration_method"] == "tiled" for r in tp)
    combo = {(r["reg_tiled_tile"], r["reg_tiled_gate_tre"]) for r in tp}
    assert combo == {(t, g) for t in (1024, 2048, 4096) for g in (0.5, 1.0, 2.0)}


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
    assert "enable_feature_error" not in sweep["baseline"]         # replaced by reg_qc=2


def test_project_sweep_baseline_matches_pipeline_defaults():
    # The 'baseline' run must be the SHIPPED default config. These mirror nextflow.config defaults
    # (as of 2026-07-27); update BOTH sides together if a pipeline default changes.
    import yaml
    base = yaml.safe_load(
        (Path(__file__).parents[1] / "configs" / "sweep.yaml").read_text())["baseline"]
    assert base["memory_mode"] == "high"               # nextflow.config default
    assert base["reg_qc"] == 2                          # nextflow.config default
    assert base["quantify_compartments"] is True       # nextflow.config default
    assert base["expanded_quantification"] is True      # nextflow.config default (Mean/Sum on top of Median)
    assert base["seg_cellsam_block_size"] == 1024       # nextflow.config default
    assert base["cse_max_pixels"] == 50000000           # nextflow.config default (CSE downsample cap)
    assert base["preproc_pool_workers"] is None         # nextflow.config default (null = task.cpus)
    assert base["reg_micro_reg"] == 0                   # nextflow.config default (micro-registration off)
    assert base["registration_method"] == "valis"       # nextflow.config default backend
    assert "skip_micro_registration" not in base        # DEAD param: the pipeline never read it


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
