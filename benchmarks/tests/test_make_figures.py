from pathlib import Path

from benchmarks.analysis import make_figures

FIX = Path(__file__).parent / "fixtures"


def test_run_produces_config_and_figures(tmp_path):
    result = make_figures.run(
        results_root=FIX / "runs",
        run_plan_csv=FIX / "runs_run_plan.csv",
        manifest_csv=FIX / "runs_matrix_manifest.csv",
        reg_eval_csv=None,
        outdir=tmp_path,
    )
    # tidy frame has the 2 runs x 2 processes
    assert len(result["runs_df"]) == 4
    # optimized config written + per-process scaling figures exist
    assert (tmp_path / "modules.optimized.config").exists()
    figs = list((tmp_path / "figures").glob("scaling_*.pdf"))
    assert len(figs) >= 1
    assert "SEGMENT" in result["models"]
