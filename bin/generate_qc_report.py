#!/usr/bin/env python3
"""
generate_qc_report.py
Generates a self-contained HTML QC report for a MIRAGE pipeline run.
All images are embedded as base64; no external dependencies required.
"""

import argparse
import base64
import csv
import glob
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Generate MIRAGE QC HTML report")
    p.add_argument("--preprocess-qc",    default="preprocess_qc/",   help="Directory of preprocessing QC PNGs")
    p.add_argument("--registration-qc",  default="registration_qc/", help="Directory of registration QC PNGs")
    p.add_argument("--feature-distances",default="feature_dist/",     help="Directory of feature-distance JSONs")
    p.add_argument("--valis-summary",    default="valis_summary/",    help="Directory of Valis summary CSVs")
    p.add_argument("--postprocess-qc",   default="postprocess_qc/",   help="Directory of postprocessing QC PNGs")
    p.add_argument("--versions",         default=None,                help="Path to collated versions.yml")
    p.add_argument("--output",           default="mirage_qc_report.html", help="Output HTML path")
    p.add_argument("--data-dir",         default="mirage_qc_data/",   help="Directory to copy input data into")
    return p.parse_args()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def list_files(directory, pattern="*"):
    """Return sorted list of existing files matching glob pattern inside directory."""
    d = Path(directory)
    if not d.exists():
        return []
    return sorted(d.glob(pattern))


def img_to_b64(path):
    """Return a data URI string for an image file (PNG or TIFF)."""
    ext = Path(path).suffix.lower()
    mime_types = {
        ".png": "image/png",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }
    mime = mime_types.get(ext, "image/png")
    with open(path, "rb") as fh:
        data = base64.b64encode(fh.read()).decode("ascii")
    return f"data:{mime};base64,{data}"


def parse_valis_summary(csv_path):
    """
    Parse a preprocessed_summary.csv (or similar) produced by Valis.
    Returns (headers, rows) where each row is a list of strings.
    Shows all columns present in the CSV (including rTRE, D, n_matches, etc.).
    """
    with open(csv_path, newline="") as fh:
        reader = csv.DictReader(fh)
        display_headers = reader.fieldnames or []
        rows = []
        for row in reader:
            rows.append([row.get(h, "") for h in display_headers])
    return display_headers, rows


def parse_feature_dist_json(json_path):
    """
    Parse a feature-distance JSON.
    Expected structure:
      { "before": {"mean": float, ...}, "after": {"mean": float, ...}, ... }
    Returns a dict summary.
    """
    with open(json_path) as fh:
        data = json.load(fh)
    before_mean = None
    after_mean  = None
    if isinstance(data, dict):
        # Use `is not None` (not `or`): a perfectly-aligned panel can have a
        # before/after mean of 0, which `or` would wrongly treat as missing.
        before = data["before"] if data.get("before") is not None else data.get("pre") or {}
        after  = data["after"]  if data.get("after")  is not None else data.get("post") or {}
        if isinstance(before, dict):
            before_mean = before.get("mean")
        if isinstance(after, dict):
            after_mean = after.get("mean")
    improvement = None
    if before_mean is not None and after_mean is not None and before_mean != 0:
        improvement = round((1 - after_mean / before_mean) * 100, 2)
    return {
        "file": Path(json_path).name,
        "before_mean": before_mean,
        "after_mean":  after_mean,
        "improvement_pct": improvement,
    }

# ---------------------------------------------------------------------------
# HTML building blocks
# ---------------------------------------------------------------------------

CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: 'Segoe UI', Arial, sans-serif;
    background: #f4f6f9;
    color: #333;
    line-height: 1.5;
}
header {
    background: #1a2332;
    color: #fff;
    padding: 24px 40px;
}
header h1 { font-size: 2rem; font-weight: 700; }
header .subtitle { font-size: 0.95rem; opacity: 0.75; margin-top: 4px; }
main { max-width: 1400px; margin: 32px auto; padding: 0 24px 60px; }
section { background: #fff; border-radius: 8px; box-shadow: 0 1px 4px rgba(0,0,0,.1);
          margin-bottom: 32px; overflow: hidden; }
section h2 {
    background: #2c3e50;
    color: #fff;
    padding: 14px 20px;
    font-size: 1.15rem;
    font-weight: 600;
}
.section-body { padding: 20px; }
table { border-collapse: collapse; width: 100%; font-size: 0.9rem; }
th { background: #ecf0f1; text-align: left; padding: 8px 12px; font-weight: 600;
     border-bottom: 2px solid #bdc3c7; }
td { padding: 7px 12px; border-bottom: 1px solid #ecf0f1; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: #f8f9fa; }
.img-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 16px;
    margin-top: 8px;
}
.img-grid-wide {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(500px, 1fr));
    gap: 20px;
    margin-top: 8px;
}
.img-card {
    border: 1px solid #dde2ea;
    border-radius: 6px;
    overflow: hidden;
    background: #fafbfc;
}
.img-card img { width: 100%; display: block; }
.img-card .caption {
    font-size: 0.78rem;
    padding: 6px 8px;
    text-align: center;
    color: #555;
    word-break: break-all;
}
.empty-notice {
    color: #888;
    font-style: italic;
    padding: 8px 0;
}
"""


def html_header(timestamp):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>MIRAGE QC Report</title>
<style>{CSS}</style>
</head>
<body>
<header>
  <h1>MIRAGE QC Report</h1>
  <div class="subtitle">Generated: {timestamp}</div>
</header>
<main>
"""


def html_footer():
    return "</main>\n</body>\n</html>\n"


def section(title, body_html):
    return (
        f'<section>\n'
        f'  <h2>{title}</h2>\n'
        f'  <div class="section-body">\n{body_html}\n  </div>\n'
        f'</section>\n'
    )


def img_grid(png_files, wide=False):
    if not png_files:
        return '<p class="empty-notice">No images found.</p>'
    grid_class = "img-grid-wide" if wide else "img-grid"
    cards = []
    for p in png_files:
        b64 = img_to_b64(p)
        name = Path(p).name
        cards.append(
            f'<div class="img-card">'
            f'<img src="{b64}" alt="{name}" loading="lazy">'
            f'<div class="caption">{name}</div>'
            f'</div>'
        )
    return f'<div class="{grid_class}">' + "".join(cards) + "</div>"


def preprocess_qc_section(preprocess_dir):
    pngs = list_files(preprocess_dir, "*.png")
    return section("Preprocessing QC", img_grid(pngs))


def registration_qc_section(reg_dir, feat_dir, valis_dir):
    parts = []

    # Image grid
    pngs = list_files(reg_dir, "*.png")
    parts.append("<h3 style='margin-bottom:8px;font-size:1rem;color:#444;'>Overlay Images</h3>")
    parts.append(img_grid(pngs))

    # Valis rTRE table
    valis_csvs = list_files(valis_dir, "*.csv")
    if valis_csvs:
        parts.append("<h3 style='margin:20px 0 8px;font-size:1rem;color:#444;'>Registration Accuracy (Valis rTRE)</h3>")
        for csv_path in valis_csvs:
            try:
                headers, rows = parse_valis_summary(csv_path)
            except Exception as exc:
                parts.append(f'<p class="empty-notice">Could not parse {Path(csv_path).name}: {exc}</p>')
                continue
            parts.append(f"<p style='font-size:0.85rem;color:#666;margin-bottom:6px;'>{Path(csv_path).name}</p>")
            tbl = "<table><thead><tr>" + "".join(f"<th>{h}</th>" for h in headers) + "</tr></thead><tbody>"
            for row in rows:
                tbl += "<tr>" + "".join(f"<td>{v}</td>" for v in row) + "</tr>"
            tbl += "</tbody></table>"
            parts.append(tbl)
    else:
        parts.append('<p class="empty-notice" style="margin-top:12px;">No Valis summary CSVs found.</p>')

    # Feature distance table
    feat_jsons = list_files(feat_dir, "*.json")
    if feat_jsons:
        parts.append("<h3 style='margin:20px 0 8px;font-size:1rem;color:#444;'>Feature Distances</h3>")
        tbl = (
            "<table><thead><tr>"
            "<th>File</th><th>Before Mean</th><th>After Mean</th><th>Improvement (%)</th>"
            "</tr></thead><tbody>"
        )
        for jp in feat_jsons:
            try:
                info = parse_feature_dist_json(jp)
            except Exception as exc:
                tbl += f"<tr><td>{Path(jp).name}</td><td colspan='3'>Parse error: {exc}</td></tr>"
                continue
            bm  = f"{info['before_mean']:.4f}" if info["before_mean"] is not None else "N/A"
            am  = f"{info['after_mean']:.4f}"  if info["after_mean"]  is not None else "N/A"
            imp = f"{info['improvement_pct']:.2f}" if info["improvement_pct"] is not None else "N/A"
            tbl += f"<tr><td>{info['file']}</td><td>{bm}</td><td>{am}</td><td>{imp}</td></tr>"
        tbl += "</tbody></table>"
        parts.append(tbl)
    else:
        parts.append('<p class="empty-notice" style="margin-top:12px;">No feature-distance JSONs found.</p>')

    return section("Registration QC", "\n".join(parts))


def postprocess_qc_section(postprocess_dir):
    # Exclude seg_overlay images, keep only histograms/stats
    all_pngs = list_files(postprocess_dir, "*.png")
    pngs = [p for p in all_pngs if "_seg_overlay" not in p.name]
    return section("Postprocessing QC", img_grid(pngs, wide=True))

# ---------------------------------------------------------------------------
# Data copy
# ---------------------------------------------------------------------------

def copy_data(args):
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    def copy_glob(src_dir, pattern, subdir):
        files = list_files(src_dir, pattern)
        if not files:
            return
        dest = data_dir / subdir
        dest.mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.copy2(f, dest / Path(f).name)

    copy_glob(args.preprocess_qc,        "*.png",  "preprocess_qc")
    copy_glob(args.registration_qc,      "*.png",  "registration_qc")
    copy_glob(args.feature_distances,    "*.json", "feature_dist")
    copy_glob(args.valis_summary,        "*.csv",  "valis_summary")
    copy_glob(args.postprocess_qc,       "*.png",  "postprocess_qc")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    html_parts = [html_header(timestamp)]
    html_parts.append(preprocess_qc_section(args.preprocess_qc))
    html_parts.append(registration_qc_section(
        args.registration_qc,
        args.feature_distances,
        args.valis_summary,
    ))
    html_parts.append(postprocess_qc_section(args.postprocess_qc))
    html_parts.append(html_footer())

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("".join(html_parts))

    print(f"[generate_qc_report] Report written to: {out_path}")

    copy_data(args)
    print(f"[generate_qc_report] Data copied to: {args.data_dir}")


if __name__ == "__main__":
    main()
