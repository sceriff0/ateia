import matplotlib
matplotlib.use("Agg")  # headless

import numpy as np
import pandas as pd

from benchmarks.analysis.lib import plotting


def test_save_fig_writes_pdf_and_svg(tmp_path):
    import matplotlib.pyplot as plt
    plotting.set_paper_theme()
    fig, ax = plt.subplots()
    ax.plot([0, 1], [0, 1])
    paths = plotting.save_fig(fig, tmp_path / "fig1")
    assert (tmp_path / "fig1.pdf").exists() and (tmp_path / "fig1.svg").exists()
    assert (tmp_path / "fig1.pdf").stat().st_size > 0
    assert set(paths) == {tmp_path / "fig1.pdf", tmp_path / "fig1.svg"}


def test_scatter_with_fit_returns_figure(tmp_path):
    x = np.array([1.0, 2.0, 3.0, 4.0])
    y = 2.0 * x + 1.0
    fig = plotting.scatter_with_fit(x, y, slope=2.0, intercept=1.0,
                                    xlabel="input (GB)", ylabel="peak RSS (GB)",
                                    title="SEGMENT")
    assert fig is not None
    plotting.save_fig(fig, tmp_path / "scatter")  # must not raise
    assert (tmp_path / "scatter.pdf").exists()


def test_before_after_box_returns_figure():
    df = pd.DataFrame({"original": [0.1, 0.2, 0.3], "registered": [0.02, 0.03, 0.04]})
    fig = plotting.before_after_box(df, cols=["original", "registered"],
                                    ylabel="rTRE", title="tiled")
    assert fig is not None
