import importlib.util
import sys
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
    # That module imports its sibling `_code_view` flat, the way everything in tests/
    # does -- but loading a file by path does not put its directory on sys.path, so the
    # sibling import fails with ModuleNotFoundError before a single assertion runs.
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
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


def test_project_sweep_exercises_the_tiled_fanout_path():
    """The STARE fan-out (TILED_COARSE/REG_TILE/SOLVE/STITCH) must be measured.

    These four processes only ever run under registration_method=tiled, so if the plan carries no
    tiled runs they are never benchmarked at all. This used to also assert that both sides of the
    old execution-shape flag appeared; that flag was removed along with the single-task
    TILED_REGISTER path, so the fan-out is the only shape and there is nothing left to cross.
    """
    import yaml
    sweep = yaml.safe_load(
        (Path(__file__).parents[1] / "configs" / "sweep.yaml").read_text())
    plan = build_run_plan(sweep, repeats=1)
    tiled = [r for r in plan if r.get("registration_method") == "tiled"]
    assert tiled, "no tiled/STARE runs in the plan"


def test_project_sweep_covers_every_shipped_segmentation_backend():
    import yaml
    sweep = yaml.safe_load(
        (Path(__file__).parents[1] / "configs" / "sweep.yaml").read_text())
    # nextflow.config's seg_method enum -- all three must be benchmarked somewhere in the plan.
    plan = build_run_plan(sweep, repeats=1)
    assert {r["seg_method"] for r in plan} == {"stardist", "instantseg", "cellsam"}


def test_project_stare_resolution_axis_mirrors_the_valis_one():
    """Both methods' transform-resolution knob must be swept, or the head-to-head is biased.

    reg_max_image_dim (VALIS) and reg_tiled_coarse_max_dim (STARE) are the same thing for their
    respective methods: the resolution the global transform is solved at, and the dominant
    runtime-and-accuracy axis of each. Sweeping one and holding the other at its default compares
    a TUNED VALIS against an UNTUNED STARE -- a bias that is invisible in the output tables.
    """
    import yaml
    sweep = yaml.safe_load(
        (Path(__file__).parents[1] / "configs" / "sweep.yaml").read_text())
    valis_swept = "reg_max_image_dim" in sweep.get("axes", {})
    stare_values = set(
        sweep["registration_method_grid"]["tiled"].get("reg_tiled_coarse_max_dim", []))
    assert valis_swept, "reg_max_image_dim is no longer swept — re-check this test's premise"
    assert len(stare_values) >= 3, (
        "reg_max_image_dim is swept but reg_tiled_coarse_max_dim is not (or has <3 points): "
        f"{sorted(stare_values)}"
    )
    base = sweep["baseline"]["reg_tiled_coarse_max_dim"]
    assert min(stare_values) < base < max(stare_values), (
        f"the STARE resolution axis must bracket its default {base} in both directions, "
        f"as reg_max_image_dim does: {sorted(stare_values)}"
    )


def test_project_sweep_has_no_dead_axes():
    # An OFAT axis on a param that is gated behind another param is a no-op run. These belong in
    # a per-method grid (gate pinned on), never in flat `axes`.
    import yaml
    sweep = yaml.safe_load(
        (Path(__file__).parents[1] / "configs" / "sweep.yaml").read_text())
    gated = {
        "reg_tiled_tile", "reg_tiled_gate_tre",                        # registration_method=tiled
        "reg_tiled_halo", "reg_tiled_upsample", "reg_tiled_out_tile",
        "reg_tiled_coarse_max_dim",
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


# ---------------------------------------------------------------------------
# Coverage registry: every pipeline param is either SWEPT or excluded here with
# a reason. test_every_pipeline_param_is_swept_or_excused fails on any param that
# is in neither set, so a param added to nextflow.config cannot silently escape
# the benchmark. This is the coverage counterpart to
# test_project_sweep_baseline_matches_pipeline_defaults (which pins the values).
# ---------------------------------------------------------------------------
NOT_SWEPT = {
    # --- run identity / where the data is. Not knobs: they select the job, not its cost. ---
    "input": "samplesheet path — supplied per run by run_sweep.sh",
    "outdir": "output path — supplied per run by run_sweep.sh",
    "prior_outdir": "add_cycle input; the sweep benchmarks standard mode only",
    "mode": "'standard' vs 'add_cycle' — a different PIPELINE, not a knob (see docs/add_cycle.md)",
    "start": "step gate — every sweep run is a full pipeline; a partial run is not comparable",
    "stop": "step gate — see start",

    # --- site / scheduler. Properties of the cluster, not of the pipeline. ---
    "max_cpus": "resourceLimits clamp — a site ceiling; varying it benchmarks the machine",
    "max_memory": "resourceLimits clamp — see max_cpus",
    "max_time": "resourceLimits clamp — see max_cpus",
    "max_forks": (
        "cluster concurrency, not pipeline cost. It caps how many tasks of one process run "
        "at once, so varying it changes how fast the SWEEP drains, never what a task costs. "
        "Same category as max_cpus/max_memory/max_time: it benchmarks the machine. It is also "
        "read EAGERLY at conf/modules.config parse time (Math.min(own, params.max_forks)), so "
        "run_sweep.sh must pass it on the CLI -- a -c file sets it too late to reach the clamps."),
    "queue_size": (
        "cluster concurrency, not pipeline cost -- see max_forks. The two are a pair and the "
        "lower binds, so benchmark.config raises both together on the CLI."),
    "slurm_account": "scheduler identity",
    "slurm_partition": "scheduler identity",
    "slurm_qos": "scheduler identity",
    "gpu_type": "site GPU selector — the hardware under test, not a pipeline knob",

    # --- asset paths. Site-local files; a path string has no cost curve. ---
    "segmentation_model": "StarDist model NAME — an asset, not a knob",
    "segmentation_model_dir": "StarDist model dir — site-local asset path",
    "instanseg_model_dir": "InstanSeg model dir — site-local asset path",
    "cellsam_model_path": "CellSAM weights — site-local asset path",

    # The automated-phenotyping params (panel_spec, panel_model, pheno_alpha,
    # pheno_max_enumerate, pheno_min_cal) used to be excused here. They left
    # nextflow.config when that feature was extracted from main, and this registry is
    # not allowed to carry excuses for params that no longer exist -- a dead excuse is
    # indistinguishable from a deliberate coverage gap. If phenotyping returns, the
    # other direction of this guard will demand they be re-added.

    # --- properties of the INPUT DATA, fixed by generate_matrix.py. ---
    "pixel_size": "a property of the synthetic images, set by the matrix generator",
    "nuclear_markers": "channel naming contract — the matrix generator fixes the panel",
    "allow_auto_reference": "samplesheet semantics — the generated sheet always names a reference",

    # --- the CSE segmentation-quality scorer (restored on this branch, opt-in). ---
    # Not a cost knob of the PIPELINE: SEG_QUALITY_EVAL is a scorer bolted onto the
    # end, and the synthetic sweep cannot exercise it meaningfully anyway — CSE's
    # metrics assume multiple cell types differing in channel expression, and the
    # matrix's extra channels are channel 0 duplicated with jitter. It is measured
    # on REAL slides by the segmentation arm in benchmarks/configs/arms.yaml
    # (score_with: cse), which is where a segmentation-quality number is defensible.
    "skip_seg_quality_eval": "opt-in scorer; exercised by arms.yaml's segmentation arm, not the synthetic sweep",
    "cse_pixel_size_um": "an image property, not a knob — inferred from metadata when null",
    "cse_max_pixels": (
        "a MEASUREMENT setting, and not a comparable one: the composite QualityScore "
        "shifts with the binning factor, so sweeping it would produce scores that "
        "cannot be read against each other. Held fixed across a cohort by design."),
    "segeval_tag": "container tag — an asset selector, not a cost knob",

    # --- developer / trace plumbing. ---
    "debug_channels": "debug output only",
    "dry_run": "dry_run does not execute the pipeline",
    "enable_trace": "held ON — the sweep's own measurement plumbing (size_logs feed the regression)",
    "trace_dir": "trace output path",

    # --- gated behind a backend/method choice: a flat OFAT run would be a no-op. ---
    # Same rule test_project_sweep_has_no_dead_axes enforces: pin the gate, cross the knob.
    "seg_pmin": "seg_method=stardist only (starDistCommonFlags) — belongs in segmentation_grid.stardist",
    "seg_pmax": "seg_method=stardist only — see seg_pmin",
    "seg_prob_thresh": "seg_method=stardist only — see seg_pmin",
    "seg_expand_distance": (
        "reaches SEGMENT only via starDistCommonFlags; at the instantseg baseline it would move "
        "only SEG_QC_GEOJSON's contours — i.e. change the accuracy MEASUREMENT rather than the "
        "pipeline's own segmentation. Belongs in segmentation_grid.stardist."),
    "seg_instantseg_model": "seg_method=instantseg only — an asset name, not a cost knob",
    "seg_instantseg_target": "seg_method=instantseg only — selects which outputs are produced",
    "seg_cellsam_overlap": "seg_method=cellsam only — add to segmentation_grid.cellsam if needed",
    "seg_cellsam_use_wsi": "seg_method=cellsam only — see seg_cellsam_overlap",
    "reg_tiled_halo": "registration_method=tiled only — secondary accuracy knob, held at default",
    "reg_tiled_upsample": "registration_method=tiled only — see reg_tiled_halo",
    "reg_tiled_out_tile": "registration_method=tiled only — write-side I/O, held at default",
    "reg_tiled_nuclear_index": (
        "registration_method=tiled only — a channel index, not a cost knob. Renamed "
        "from reg_tiled_dapi_index, and its default is now null (resolve from the "
        "slide's channel metadata) rather than 0; sweeping it would only re-test "
        "MarkerUtils' resolution, which tests/ already covers."
    ),

    # --- secondary knobs deliberately held at defaults (add an axis if a curve is ever needed). ---
    "reg_micro_reg_fraction": "secondary micro-registration knob; reg_micro_reg (the DEPTH) is crossed instead",
    "preprocess_qc_scale_factor": "preprocess-QC render scale; qc_scale_factor already prices QC rendering",
    "spatialdata_residual_join_max_px": "join tolerance — changes the export's matching, not its cost",
    "embed_masks": "embeds masks in the OME-TIFF; a niche output-format toggle",

    # --- QC gates held ON: the paper's quality tables are harvested from these artifacts. ---
    # Flipping them off yields runs absent from every quality table (same caveat as reg_qc<2).
    "skip_preprocess_qc": "held ON — turning QC off drops the run from the quality tables",
    "skip_registration_qc": "held ON — see skip_preprocess_qc",
    "skip_postprocessing_qc": "held ON — see skip_preprocess_qc",
    "skip_final_qc_report": "held ON — the final report is where versions/size_logs are aggregated",
}


def _sweep_covered_params(sweep) -> set:
    """Every pipeline param the sweep touches, in the baseline or any axis/grid."""
    covered = set(sweep["baseline"]) | set(sweep.get("axes", {}))
    for grid in ("scaling_grid", "registration_grid", "registration_param_grid"):
        covered |= set(sweep.get(grid, {}))
    for grid in ("segmentation_grid", "registration_method_grid"):
        for per_method in sweep.get(grid, {}).values():
            covered |= set(per_method)
    return covered


def test_every_pipeline_param_is_swept_or_excused():
    """No pipeline param may be silently unbenchmarked.

    The benchmark's claim is that it characterises the pipeline. A param that is neither
    swept nor listed in NOT_SWEPT is a hole in that claim -- and the failure mode is silent,
    because nothing else in the suite reads nextflow.config's full param list. Adding a param
    to nextflow.config now forces a decision here: sweep it, or say why not.
    """
    import yaml
    sweep = yaml.safe_load(
        (Path(__file__).parents[1] / "configs" / "sweep.yaml").read_text())
    params = set(pipeline_param_defaults())

    unaccounted = params - _sweep_covered_params(sweep) - set(NOT_SWEPT)
    assert not unaccounted, (
        "pipeline params neither swept nor excused: " + ", ".join(sorted(unaccounted)) +
        "\nEither add an axis/baseline entry in benchmarks/configs/sweep.yaml, or add the "
        "param to NOT_SWEPT with the reason it is not benchmarkable."
    )


def test_not_swept_registry_has_no_stale_entries():
    """NOT_SWEPT must not excuse a param that no longer exists, or one now swept.

    Without this, a deleted param leaves a dead excuse behind (and a param later promoted to
    a real axis keeps a stale 'not benchmarked' note contradicting the sweep).
    """
    import yaml
    sweep = yaml.safe_load(
        (Path(__file__).parents[1] / "configs" / "sweep.yaml").read_text())
    params = set(pipeline_param_defaults())

    ghosts = set(NOT_SWEPT) - params
    assert not ghosts, f"NOT_SWEPT excuses params that no longer exist in nextflow.config: {sorted(ghosts)}"

    contradictory = set(NOT_SWEPT) & _sweep_covered_params(sweep)
    assert not contradictory, (
        f"params are BOTH swept and listed as not-swept: {sorted(contradictory)} -- drop the NOT_SWEPT entry"
    )


def test_every_not_swept_entry_states_a_reason():
    for name, reason in NOT_SWEPT.items():
        assert isinstance(reason, str) and len(reason) > 15, (
            f"NOT_SWEPT['{name}'] must state a real reason, got {reason!r}")
