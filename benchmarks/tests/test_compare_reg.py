import pandas as pd

from benchmarks.analysis.lib.compare_reg import compare_classic_vs_distributed


def _run_rows(run_id, dist, procs, target_px=4096, n_channels=2, n_register_images=2, force_tiling=False):
    """procs: list of (leaf, peak_rss_gb, realtime_s). Cell = (target_px, n_channels, n_register_images)."""
    return pd.DataFrame([
        {"process": f"MIRAGE:REGISTRATION:{leaf}", "peak_rss_gb": rss, "realtime_s": rt,
         "config_id": run_id, "run_id": run_id, "rep": 0, "reg_distributed_tiling": dist,
         "reg_dist_force_tiling": force_tiling,
         "target_px": target_px, "n_channels": n_channels, "n_register_images": n_register_images}
        for leaf, rss, rt in procs
    ])


def test_tiled_distributed_excluded_from_classic_comparison():
    # tiled path (force_tiling=true) is a different algorithm from classic whole-image -> must NOT be
    # paired with classic (would be a false mismatch). Only the SEPARATED distributed run pairs.
    classic = _run_rows("c", False, [("VALIS_ADAPTER:REGISTER", 60.0, 100.0)])
    sep = _run_rows("d", True, [("REG_PREP", 20.0, 30.0), ("REG_FINALIZE_FIELD", 25.0, 20.0)], force_tiling=False)
    tiled = _run_rows("t", True, [("REG_PREP", 5.0, 30.0), ("REG_FINALIZE", 6.0, 20.0)], force_tiling=True)
    out = compare_classic_vs_distributed(pd.concat([classic, sep, tiled], ignore_index=True))
    assert len(out) == 1                                   # classic vs SEPARATED only
    assert out.iloc[0]["reg_peak_rss_gb_distributed"] == 25.0   # the separated run, not the tiled one


def test_pairs_same_cell_and_reports_rss_saving():
    classic = _run_rows("run0000", False, [("VALIS_ADAPTER:REGISTER", 60.0, 100.0)])
    dist = _run_rows("run0046", True, [
        ("REG_PREP", 20.0, 30.0),
        ("REG_NONRIGID", 8.0, 40.0),
        ("REG_FINALIZE_FIELD", 25.0, 20.0),
        ("REG_WARP_REF", 15.0, 10.0),
    ])
    out = compare_classic_vs_distributed(pd.concat([classic, dist], ignore_index=True))
    assert len(out) == 1
    row = out.iloc[0]
    # classic peak = 60 (single REGISTER); distributed peak = MAX across stage = 25 (FINALIZE_FIELD)
    assert row["reg_peak_rss_gb_classic"] == 60.0
    assert row["reg_peak_rss_gb_distributed"] == 25.0
    assert row["rss_saving_gb"] == 35.0
    assert row["reg_total_realtime_s_distributed"] == 100.0  # 30+40+20+10
    assert row["peak_rss_ratio"] == 25.0 / 60.0


def test_pairs_per_cell_across_sizes():
    # distributed_grid across two sizes; classic counterpart at each size
    rows = pd.concat([
        _run_rows("c2048", False, [("VALIS_ADAPTER:REGISTER", 10.0, 50.0)], target_px=2048),
        _run_rows("d2048", True, [("REG_PREP", 4.0, 20.0), ("REG_FINALIZE_FIELD", 5.0, 30.0)], target_px=2048),
        _run_rows("c8192", False, [("VALIS_ADAPTER:REGISTER", 90.0, 400.0)], target_px=8192),
        _run_rows("d8192", True, [("REG_PREP", 20.0, 150.0), ("REG_FINALIZE_FIELD", 22.0, 200.0)], target_px=8192),
    ], ignore_index=True)
    out = compare_classic_vs_distributed(rows).set_index("target_px")
    assert set(out.index) == {2048, 8192}
    # the RAM saving grows with size (the whole point of the distributed_grid)
    assert out.loc[2048, "rss_saving_gb"] == 10.0 - 5.0
    assert out.loc[8192, "rss_saving_gb"] == 90.0 - 22.0
    assert out.loc[8192, "rss_saving_gb"] > out.loc[2048, "rss_saving_gb"]


def test_cell_measured_only_one_way_is_not_paired():
    rows = pd.concat([
        _run_rows("c4096", False, [("VALIS_ADAPTER:REGISTER", 60.0, 100.0)], target_px=4096),
        _run_rows("d8192", True, [("REG_PREP", 20.0, 30.0)], target_px=8192),  # distributed only, no classic
    ], ignore_index=True)
    assert compare_classic_vs_distributed(rows).empty


def test_pairing_restricted_to_purpose_built_grids():
    # registration_param_grid also has classic+distributed at the baseline cell (varied memory_mode) —
    # they must NOT pollute the size-crossed classic-vs-distributed saving. Only baseline/scaling_grid
    # (classic) vs distributed_grid (distributed) are paired.
    def rows(run_id, va, dist, rss):
        return pd.DataFrame([{"process": "MIRAGE:REGISTRATION:" + ("REG_PREP" if dist else "VALIS_ADAPTER:REGISTER"),
                              "peak_rss_gb": rss, "realtime_s": 100.0, "config_id": run_id, "run_id": run_id,
                              "rep": 0, "reg_distributed_tiling": dist, "reg_dist_force_tiling": False,
                              "varied_axis": va, "target_px": 4096, "n_channels": 2, "n_register_images": 2}])
    df = pd.concat([
        rows("base", "baseline", False, 60.0),
        rows("dg", "distributed_grid", True, 25.0),
        rows("rp_c", "registration_param_grid", False, 200.0),   # noise that must be excluded
        rows("rp_d", "registration_param_grid", True, 5.0),
    ], ignore_index=True)
    out = compare_classic_vs_distributed(df)
    assert len(out) == 1
    assert out.iloc[0]["reg_peak_rss_gb_classic"] == 60.0        # baseline, not the 200 param-grid run
    assert out.iloc[0]["reg_peak_rss_gb_distributed"] == 25.0    # distributed_grid, not the 5 param-grid run


def test_empty_when_no_distributed_runs():
    classic = _run_rows("run0000", False, [("VALIS_ADAPTER:REGISTER", 60.0, 100.0)])
    assert compare_classic_vs_distributed(classic).empty


def test_truthy_handles_nextflow_string_booleans():
    classic = _run_rows("run0000", "false", [("VALIS_ADAPTER:REGISTER", 60.0, 100.0)])
    dist = _run_rows("run0046", "true", [("REG_PREP", 20.0, 30.0)])
    out = compare_classic_vs_distributed(pd.concat([classic, dist], ignore_index=True))
    assert len(out) == 1
    assert out.iloc[0]["reg_peak_rss_gb_distributed"] == 20.0
