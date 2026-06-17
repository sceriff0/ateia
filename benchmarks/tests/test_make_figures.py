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


def test_notebook_executes_on_fixture(tmp_path):
    """Smoke: the notebook's lib calls run headless against the fixture.

    We execute the analysis path (not nbconvert, to stay dependency-light) by
    re-running make_figures, which mirrors notebook sections 1-3.
    """
    res = make_figures.run(
        results_root=FIX / "runs", run_plan_csv=FIX / "runs_run_plan.csv",
        manifest_csv=FIX / "runs_matrix_manifest.csv", reg_eval_csv=None, outdir=tmp_path,
    )
    assert (tmp_path / "modules.optimized.config").read_text().count("withName") >= 2


def test_notebook_file_exists_and_is_valid():
    import nbformat
    from pathlib import Path
    nb_path = Path(__file__).parents[1] / "analysis" / "benchmark_analysis.ipynb"
    nb = nbformat.read(str(nb_path), as_version=4)
    assert len(nb.cells) >= 8  # 5 sections + intro + setup
