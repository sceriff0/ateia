"""Generate benchmark_analysis.ipynb (thin presentation layer over the lib)."""
from pathlib import Path

import nbformat as nbf

CELLS = [
    ("md", "# Mirage Benchmark Analysis\n\n"
           "Resource scaling + regression, the regression-derived `modules.config`, "
           "and registration accuracy (tiled vs classic). Set `RESULTS_ROOT` etc. below. "
           "All logic lives in `benchmarks/analysis/lib` (tested)."),
    ("code", "import matplotlib\n%matplotlib inline\n"
             "from pathlib import Path\n"
             "from benchmarks.analysis.lib import load, regress, emit_config, plotting\n"
             "from benchmarks.analysis import make_figures\n"
             "plotting.set_paper_theme()\n"
             "# EDIT THESE to your sweep outputs:\n"
             "RESULTS_ROOT = Path('../../bench_results')\n"
             "RUN_PLAN = Path('../../bench_run_plan.csv')\n"
             "MANIFEST = Path('../../bench_matrix/matrix_manifest.csv')\n"
             "REG_EVAL = Path('../../reg_eval.csv')  # from Plan 2; may not exist yet"),
    ("md", "## 1. Load + tidy"),
    ("code", "df = load.load_runs(RESULTS_ROOT, RUN_PLAN, MANIFEST)\n"
             "print(df.shape)\ndf.head()"),
    ("md", "## 2. Resource ~ input-size regression (per process)"),
    ("code", "models = regress.fit_per_process(df, predictor='input_gb', target='peak_rss_gb')\n"
             "for p, m in sorted(models.items()):\n"
             "    print(f\"{p:24s} slope={m['slope']:.2f} intercept={m['intercept']:.2f} "
             "r2={m['r2']:.2f} sigma={m['sigma']:.2f} n={m['n']}\")"),
    ("code", "for proc, g in df.groupby('process'):\n"
             "    sub = g[['input_gb','peak_rss_gb']].dropna()\n"
             "    if sub.empty: continue\n"
             "    m = models[proc]\n"
             "    fig = plotting.scatter_with_fit(sub['input_gb'], sub['peak_rss_gb'],\n"
             "        m['slope'], m['intercept'], 'input (GiB)', 'peak RSS (GiB)', proc)\n"
             "    plotting.save_fig(fig, Path('figures')/f'scaling_{proc}')"),
    ("md", "## 3. Optimal modules.config"),
    ("code", "emit_config.write_optimized_config(models, '../../conf/modules.optimized.config')\n"
             "print(open('../../conf/modules.optimized.config').read())"),
    ("md", "## 4. Registration accuracy — tiled vs classic (needs Plan 2 reg_eval.csv)"),
    ("code", "import pandas as pd\n"
             "if REG_EVAL.exists():\n"
             "    reg = pd.read_csv(REG_EVAL)\n"
             "    piv = reg.pivot_table(index='pair_id', columns='mode', values='true_median_rtre')\n"
             "    fig = plotting.before_after_box(piv, cols=list(piv.columns),\n"
             "        ylabel='median rTRE', title='tiled vs untiled')\n"
             "    plotting.save_fig(fig, Path('figures')/'rtre_tiled_vs_untiled')\n"
             "    display(reg.groupby('mode')[['true_median_rtre','valis_rtre','feature_median']].median())\n"
             "else:\n"
             "    print('reg_eval.csv not found - run Plan 2 first')"),
    ("md", "## 5. Sampling — paired tiled-vs-untiled significance"),
    ("code", "from benchmarks.registration_eval import sampling\n"
             "if REG_EVAL.exists():\n"
             "    reg = pd.read_csv(REG_EVAL)\n"
             "    piv = reg.pivot_table(index='pair_id', columns='mode', values='true_median_px').dropna()\n"
             "    if {'tiled','untiled'} <= set(piv.columns):\n"
             "        print(sampling.paired_diff_test(piv['tiled'].values, piv['untiled'].values))"),
]


def build(path):
    nb = nbf.v4.new_notebook()
    nb.cells = [nbf.v4.new_markdown_cell(s) if k == "md" else nbf.v4.new_code_cell(s)
                for k, s in CELLS]
    nbf.write(nb, str(path))


if __name__ == "__main__":
    build(Path(__file__).parent / "benchmark_analysis.ipynb")
