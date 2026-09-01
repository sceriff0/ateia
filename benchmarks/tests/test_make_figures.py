from pathlib import Path

from benchmarks.analysis import make_figures

FIX = Path(__file__).parent / "fixtures"


def test_run_produces_config_and_figures(tmp_path):
    result = make_figures.run(
        results_root=FIX / "runs",
        run_plan_csv=FIX / "runs_run_plan.csv",
        reg_eval_csv=FIX / "reg_eval_min.csv",
        outdir=tmp_path,
    )
    # tidy frame has the 2 runs x 2 processes
    assert len(result["runs_df"]) == 4
    # optimized config written + per-process scaling figures exist
    assert (tmp_path / "modules.optimized.config").exists()
    figs = list((tmp_path / "figures").glob("scaling_*.pdf"))
    assert len(figs) >= 1
    assert "SEGMENT" in result["models"]


def test_run_writes_tidy_csvs(tmp_path):
    """The primary R-facing outputs: tidy measurements + regression fits as CSV."""
    import pandas as pd

    res = make_figures.run(
        results_root=FIX / "runs", run_plan_csv=FIX / "runs_run_plan.csv",
        reg_eval_csv=FIX / "reg_eval_min.csv", outdir=tmp_path,
    )

    meas = pd.read_csv(res["measurements_csv"])
    assert len(meas) == 4  # 2 runs x 2 processes, one row each
    # tidy long frame: measurement columns + swept params joined in
    for col in ("run_id", "process", "input_gb", "peak_rss_gb", "realtime_s",
                "cpus", "target_px", "n_channels"):
        assert col in meas.columns

    models = pd.read_csv(res["models_csv"])
    assert set(models.columns) == {
        "process", "predictor", "target", "slope", "intercept", "r2", "sigma", "n"}
    assert set(models["process"]) == {"CONVERT_IMAGE", "SEGMENT"}


def test_run_emits_an_optimized_config_with_live_blocks(tmp_path):
    """make_figures also derives resource directives from the fitted models.

    Was named test_notebook_executes_on_fixture and justified as a stand-in for
    the notebook's sections 1-3; the notebook is gone, but the assertion is about
    emit_config and is worth keeping on its own terms.
    """
    make_figures.run(
        results_root=FIX / "runs", run_plan_csv=FIX / "runs_run_plan.csv",
        reg_eval_csv=FIX / "reg_eval_min.csv", outdir=tmp_path,
    )
    assert (tmp_path / "modules.optimized.config").read_text().count("withName") >= 2


def test_run_accepts_formats_and_writes_png(tmp_path):
    # Return value deliberately unused: what `formats=` controls is which FILES land
    # on disk, which is what the globs below assert.
    make_figures.run(
        results_root=FIX / "runs", run_plan_csv=FIX / "runs_run_plan.csv",
        reg_eval_csv=FIX / "reg_eval_min.csv",
        outdir=tmp_path, formats=("png",),
    )
    pngs = list((tmp_path / "figures").glob("scaling_*.png"))
    assert len(pngs) >= 1
    assert not list((tmp_path / "figures").glob("scaling_*.pdf"))


def test_size_varying_filters_confounding_axes():
    """The memory regression must drop runs whose axis holds input size fixed
    (memory_mode, seg_gpu, ...) — only baseline/target_px/n_channels vary input."""
    import pandas as pd

    df = pd.DataFrame({
        "process": ["SEGMENT"] * 4,
        "varied_axis": ["baseline", "target_px", "n_channels", "memory_mode"],
        "input_gb": [1.0, 2.0, 3.0, 1.0],
    })
    kept = make_figures._size_varying(df)
    assert set(kept["varied_axis"]) == {"baseline", "target_px", "n_channels"}
    assert "memory_mode" not in set(kept["varied_axis"])
    # graceful fallback when the column is absent (older run plans)
    no_axis = df.drop(columns=["varied_axis"])
    assert len(make_figures._size_varying(no_axis)) == len(no_axis)


