"""Paper-ready matplotlib helpers: consistent theme + vector export (PDF+SVG).

Plot styles consolidated from notebooks/resources.ipynb (scaling scatter) and
notebooks/rTRE.ipynb (before/after boxplots).
"""
from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

_THEME = {
    "figure.figsize": (5.0, 3.5),
    "figure.dpi": 120,
    "savefig.bbox": "tight",
    "font.size": 10,
    "axes.titlesize": 11,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.alpha": 0.3,
    "legend.frameon": False,
}


def set_paper_theme() -> None:
    plt.rcParams.update(_THEME)


def save_fig(fig, path_stem) -> list[Path]:
    """Save `fig` as both .pdf and .svg (vector, paper-ready). Returns the paths."""
    stem = Path(path_stem)
    stem.parent.mkdir(parents=True, exist_ok=True)
    out = []
    for ext in ("pdf", "svg"):
        p = stem.with_suffix(f".{ext}")
        fig.savefig(p)
        out.append(p)
    plt.close(fig)
    return out


def scatter_with_fit(x, y, slope, intercept, xlabel, ylabel, title):
    x = np.asarray(x, dtype=float)
    fig, ax = plt.subplots()
    ax.scatter(x, y, s=18, alpha=0.8)
    xs = np.linspace(x.min(), x.max(), 50) if x.size else np.array([0, 1])
    ax.plot(xs, slope * xs + intercept, color="C3", lw=1.5,
            label=f"y = {slope:.2f}x + {intercept:.2f}")
    ax.set(xlabel=xlabel, ylabel=ylabel, title=title)
    ax.legend()
    return fig


def before_after_box(df, cols, ylabel, title, log_scale=True):
    fig, ax = plt.subplots()
    ax.boxplot([df[c].dropna().to_numpy() for c in cols], labels=cols)
    if log_scale:
        ax.set_yscale("log")
    ax.set(ylabel=ylabel, title=title)
    return fig
