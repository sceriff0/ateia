import pandas as pd

from benchmarks.analysis.lib.compare_reg import compare_classic_vs_distributed


def _run_rows(config_id, run_id, dist, procs):
    """procs: list of (leaf, peak_rss_gb, realtime_s)."""
    return pd.DataFrame([
        {"process": f"MIRAGE:REGISTRATION:{leaf}", "peak_rss_gb": rss, "realtime_s": rt,
         "config_id": config_id, "run_id": run_id, "rep": 0,
         "reg_distributed_tiling": dist}
        for leaf, rss, rt in procs
    ])


def test_pairs_classic_and_distributed_and_reports_rss_saving():
    classic = _run_rows("cfg000", "run0000", False, [("VALIS_ADAPTER:REGISTER", 60.0, 100.0)])
    dist = _run_rows("cfg046", "run0046", True, [
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
    # distributed total compute = 30+40+20+10 = 100
    assert row["reg_total_realtime_s_distributed"] == 100.0
    assert row["peak_rss_ratio"] == 25.0 / 60.0


def test_empty_when_no_distributed_runs():
    classic = _run_rows("cfg000", "run0000", False, [("VALIS_ADAPTER:REGISTER", 60.0, 100.0)])
    assert compare_classic_vs_distributed(classic).empty


def test_truthy_handles_nextflow_string_booleans():
    classic = _run_rows("cfg000", "run0000", "false", [("VALIS_ADAPTER:REGISTER", 60.0, 100.0)])
    dist = _run_rows("cfg046", "run0046", "true", [("REG_PREP", 20.0, 30.0)])
    out = compare_classic_vs_distributed(pd.concat([classic, dist], ignore_index=True))
    assert len(out) == 1
    assert out.iloc[0]["reg_peak_rss_gb_distributed"] == 20.0
