"""Render benchmark figures as web PNGs into the docs image dir.

Run after a real sweep to populate docs/assets/images/benchmarks/, then
uncomment the matching image slots in docs/benchmarks.md.

  python -m benchmarks.analysis.export_docs_figures \
    --results-root bench_results --run-plan bench_run_plan.csv \
    --manifest bench_matrix/matrix_manifest.csv
"""
from __future__ import annotations

import argparse
from pathlib import Path

from . import make_figures

DEFAULT_DOCS_IMG = Path("docs/assets/images/benchmarks")


def export(results_root, run_plan_csv, manifest_csv, docs_image_dir=DEFAULT_DOCS_IMG,
           reg_eval_csv=None) -> Path:
    docs_image_dir = Path(docs_image_dir)
    # make_figures writes figures under <outdir>/figures; point that at the docs dir's parent
    res = make_figures.run(results_root, run_plan_csv, manifest_csv, reg_eval_csv,
                           outdir=docs_image_dir.parent, formats=("png",))
    figdir = Path(res["outdir"]) / "figures"
    docs_image_dir.mkdir(parents=True, exist_ok=True)
    for png in figdir.glob("scaling_*.png"):
        (docs_image_dir / png.name).write_bytes(png.read_bytes())
    return docs_image_dir


def main():
    ap = argparse.ArgumentParser(description="Export benchmark figures as docs PNGs.")
    ap.add_argument("--results-root", required=True)
    ap.add_argument("--run-plan", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--reg-eval", default=None)
    ap.add_argument("--docs-image-dir", default=str(DEFAULT_DOCS_IMG))
    a = ap.parse_args()
    out = export(a.results_root, a.run_plan, a.manifest, Path(a.docs_image_dir), a.reg_eval)
    print(f"Exported docs figures to {out}")


if __name__ == "__main__":
    main()
