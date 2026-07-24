#!/usr/bin/env python3
"""
generate_qc_report.py
Generates a self-contained HTML QC report for a MIRAGE pipeline run.
All images are embedded as base64; no external dependencies required.
"""

import argparse
import base64
import csv
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "utils"))
from logger import configure_logging, get_logger  # noqa: E402

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args():
    """Parse command-line arguments."""
    p = argparse.ArgumentParser(description="Generate MIRAGE QC HTML report")
    p.add_argument(
        "--preprocess-qc",
        default="preprocess_qc/",
        help="Directory of preprocessing QC PNGs",
    )
    p.add_argument(
        "--registration-qc",
        default="registration_qc/",
        help="Directory of registration QC PNGs",
    )
    p.add_argument(
        "--feature-distances",
        default="feature_dist/",
        help="Directory of feature-distance JSONs",
    )
    p.add_argument(
        "--valis-summary",
        default="valis_summary/",
        help="Directory of Valis summary CSVs",
    )
    p.add_argument(
        "--postprocess-qc",
        default="postprocess_qc/",
        help="Directory of postprocessing QC PNGs",
    )
    p.add_argument(
        "--seg-eval",
        default=None,
        help="Directory of segmentation_metrics.csv from CSE",
    )
    p.add_argument("--versions", default=None, help="Path to collated versions.yml")
    p.add_argument("--run-summary", default=None, help="Path to run_summary.json")
    p.add_argument(
        "--distance-plots",
        default="distance_plots/",
        help="Directory of registration distance-histogram PNGs",
    )
    p.add_argument(
        "--seg-qc",
        default="seg_qc/",
        help="Directory of warp-segmentation QC JSONs",
    )
    p.add_argument("--output", default="mirage_qc_report.html", help="Output HTML path")
    p.add_argument(
        "--data-dir",
        default="mirage_qc_data/",
        help="Directory to copy input data into",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def list_files(directory, pattern="*"):
    """Return sorted list of existing files matching glob pattern inside directory."""
    if not directory:
        return []
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


def parse_seg_eval_csv(csv_path):
    """
    Parse a segmentation_metrics.csv produced by MERGE_SEG_EVAL (CSE).
    Columns are dynamic: always `id`, `QualityScore`, plus zero or more
    `metric::submetric` columns. Returns (headers, rows) like
    parse_valis_summary.
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
    after_mean = None
    if isinstance(data, dict):
        # Use `is not None` (not `or`): a perfectly-aligned panel can have a
        # before/after mean of 0, which `or` would wrongly treat as missing.
        before = (
            data["before"] if data.get("before") is not None else data.get("pre") or {}
        )
        after = (
            data["after"] if data.get("after") is not None else data.get("post") or {}
        )
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
        "after_mean": after_mean,
        "improvement_pct": improvement,
    }


def parse_versions_yml(path):
    """
    Minimal two-level YAML parser for a collated versions.yml.

    Structure (concatenated per-process blocks):
        "PROCESS:NAME":
            tool: version
    Returns {process: {tool: version}}. Stdlib-only (no PyYAML) to keep the
    report self-contained. Repeated process keys are merged.
    """
    result = {}
    current = None
    if not path or not Path(path).exists():
        return result
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            if not line.startswith((" ", "\t")):
                # top-level "PROCESS": key
                key = line.strip().rstrip(":").strip().strip('"')
                current = key
                result.setdefault(current, {})
            elif current is not None and ":" in line:
                tool, _, ver = line.strip().partition(":")
                result[current][tool.strip().strip('"')] = ver.strip().strip('"')
    return result


def versions_section(versions_path):
    """Render the collated versions.yml as a per-process software table."""
    versions = parse_versions_yml(versions_path) if versions_path else {}
    if not versions:
        body = '<p class="empty-notice">Version information not available.</p>'
        return section("Software Versions", body)
    tbl = "<table><thead><tr><th>Process</th><th>Tool</th><th>Version</th></tr></thead><tbody>"
    for proc in sorted(versions):
        tools = versions[proc]
        for i, tool in enumerate(sorted(tools)):
            proc_cell = f"<td rowspan='{len(tools)}'>{proc}</td>" if i == 0 else ""
            tbl += f"<tr>{proc_cell}<td>{tool}</td><td>{tools[tool]}</td></tr>"
    tbl += "</tbody></table>"
    return section("Software Versions", tbl)


def parse_run_summary_json(path):
    """Load the workflow-written run_summary.json; return {} on any problem."""
    if not path or not Path(path).exists():
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _kv_table(pairs):
    """Render a list of (label, value) tuples as a two-column table."""
    rows = "".join(
        f"<tr><th style='width:240px'>{k}</th><td>{'' if v is None else v}</td></tr>"
        for k, v in pairs
    )
    return f"<table><tbody>{rows}</tbody></table>"


def run_summary_section(summary):
    """Top overview card: pipeline, run context, and key parameters used."""
    if not summary:
        return section("Run Summary", '<p class="empty-notice">Run summary not available.</p>')
    pipe = summary.get("pipeline", {})
    run = summary.get("run", {})
    params = summary.get("params", {})
    pairs = [
        ("Pipeline", f"{pipe.get('name', 'mirage')} v{pipe.get('version', '?')}"),
        ("Run timestamp", run.get("timestamp")),
        ("Mode", run.get("mode")),
        ("Steps", f"{run.get('start', '?')} → {run.get('stop', '?')}"),
    ]
    for key in sorted(params):
        pairs.append((f"param: {key}", params[key]))
    return section("Run Summary", _kv_table(pairs))


def status_strip_section(present):
    """A row of stage badges: ran (green) vs no artifacts (grey)."""
    badges = []
    for stage, ran in present.items():
        color = "#27ae60" if ran else "#95a5a6"
        label = "ran" if ran else "no artifacts"
        badges.append(
            f"<span style='display:inline-block;margin:4px 8px 4px 0;padding:6px 12px;"
            f"border-radius:14px;background:{color};color:#fff;font-size:0.85rem;'>"
            f"{stage}: {label}</span>"
        )
    return section("Pipeline Stages", "<div>" + "".join(badges) + "</div>")


def manifest_section(summary):
    """Sample manifest: totals plus a per-patient image/channel table."""
    manifest = (summary or {}).get("manifest", {})
    if not manifest:
        return section("Sample Manifest", '<p class="empty-notice">Manifest not available.</p>')
    totals = manifest.get("totals", {})
    patients = manifest.get("patients", {})
    head = _kv_table([
        ("Patients", totals.get("patients")),
        ("Images", totals.get("images")),
        ("Channels", totals.get("channels")),
    ])
    tbl = ("<table style='margin-top:14px'><thead><tr>"
           "<th>Patient</th><th>Images</th><th>Channels</th>"
           "</tr></thead><tbody>")
    for pid in sorted(patients):
        row = patients[pid]
        tbl += (f"<tr><td>{pid}</td><td>{row.get('images', '')}</td>"
                f"<td>{row.get('channels', '')}</td></tr>")
    tbl += "</tbody></table>"
    return section("Sample Manifest", head + tbl)


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
    """Return the HTML document header, including the title and generation timestamp."""
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
    """Return the closing HTML tags for the report."""
    return "</main>\n</body>\n</html>\n"


def section(title, body_html):
    """Wrap ``body_html`` in a titled report section."""
    return (
        f"<section>\n"
        f"  <h2>{title}</h2>\n"
        f'  <div class="section-body">\n{body_html}\n  </div>\n'
        f"</section>\n"
    )


def img_grid(png_files, wide=False):
    """Render a list of PNG files as a grid of base64-embedded image cards."""
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
            f"</div>"
        )
    return f'<div class="{grid_class}">' + "".join(cards) + "</div>"


def preprocess_qc_section(preprocess_dir):
    """Build the preprocessing-QC report section from the PNGs in ``preprocess_dir``."""
    pngs = list_files(preprocess_dir, "*.png")
    return section("Preprocessing QC", img_grid(pngs))


def registration_qc_section(reg_dir, feat_dir, valis_dir, dist_plots_dir=None, seg_qc_dir=None):
    """Build the registration-QC report section (overlay images plus accuracy tables)."""
    parts = []

    # Image grid
    pngs = list_files(reg_dir, "*.png")
    parts.append(
        "<h3 style='margin-bottom:8px;font-size:1rem;color:#444;'>Overlay Images</h3>"
    )
    parts.append(img_grid(pngs))

    # Valis rTRE table
    valis_csvs = list_files(valis_dir, "*.csv")
    if valis_csvs:
        parts.append(
            "<h3 style='margin:20px 0 8px;font-size:1rem;color:#444;'>Registration Accuracy (Valis rTRE)</h3>"
        )
        for csv_path in valis_csvs:
            try:
                headers, rows = parse_valis_summary(csv_path)
            except Exception as exc:
                parts.append(
                    f'<p class="empty-notice">Could not parse {Path(csv_path).name}: {exc}</p>'
                )
                continue
            parts.append(
                f"<p style='font-size:0.85rem;color:#666;margin-bottom:6px;'>{Path(csv_path).name}</p>"
            )
            tbl = (
                "<table><thead><tr>"
                + "".join(f"<th>{h}</th>" for h in headers)
                + "</tr></thead><tbody>"
            )
            for row in rows:
                tbl += "<tr>" + "".join(f"<td>{v}</td>" for v in row) + "</tr>"
            tbl += "</tbody></table>"
            parts.append(tbl)
    else:
        parts.append(
            '<p class="empty-notice" style="margin-top:12px;">No Valis summary CSVs found.</p>'
        )

    # Feature distance table
    feat_jsons = list_files(feat_dir, "*.json")
    if feat_jsons:
        parts.append(
            "<h3 style='margin:20px 0 8px;font-size:1rem;color:#444;'>Feature Distances</h3>"
        )
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
            bm = (
                f"{info['before_mean']:.4f}"
                if info["before_mean"] is not None
                else "N/A"
            )
            am = (
                f"{info['after_mean']:.4f}" if info["after_mean"] is not None else "N/A"
            )
            imp = (
                f"{info['improvement_pct']:.2f}"
                if info["improvement_pct"] is not None
                else "N/A"
            )
            tbl += f"<tr><td>{info['file']}</td><td>{bm}</td><td>{am}</td><td>{imp}</td></tr>"
        tbl += "</tbody></table>"
        parts.append(tbl)
    else:
        parts.append(
            '<p class="empty-notice" style="margin-top:12px;">No feature-distance JSONs found.</p>'
        )

    # Distance-distribution histograms (previously dropped)
    dist_pngs = list_files(dist_plots_dir, "*.png") if dist_plots_dir else []
    if dist_pngs:
        parts.append(
            "<h3 style='margin:20px 0 8px;font-size:1rem;color:#444;'>Feature-Distance Histograms</h3>"
        )
        parts.append(img_grid(dist_pngs, wide=True))

    # Warp-segmentation QC metrics (previously dropped)
    seg_qc_jsons = list_files(seg_qc_dir, "*.json") if seg_qc_dir else []
    if seg_qc_jsons:
        parts.append(
            "<h3 style='margin:20px 0 8px;font-size:1rem;color:#444;'>Warp-Segmentation QC</h3>"
        )
        parts.extend(_seg_qc_table(jp) for jp in seg_qc_jsons)

    return section("Registration QC", "\n".join(parts))


def postprocess_qc_section(postprocess_dir):
    """Build the postprocessing-QC report section (histograms/stats, excluding seg overlays)."""
    # Exclude seg_overlay images, keep only histograms/stats
    all_pngs = list_files(postprocess_dir, "*.png")
    pngs = [p for p in all_pngs if "_seg_overlay" not in p.name]
    return section("Postprocessing QC", img_grid(pngs, wide=True))


def _flatten(prefix, obj, out):
    """Recursively flatten a nested dict into (dotted-key, str-value) rows."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten(f"{prefix}.{k}" if prefix else str(k), v, out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _flatten(f"{prefix}[{i}]", v, out)
    else:
        out.append((prefix, str(obj)))


def parse_seg_qc_json(path):
    """Flatten a warp-seg QC JSON into (key, value) rows; schema-agnostic."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    out = []
    _flatten("", data, out)
    return out


def _seg_qc_table(json_path):
    """
    Render a single warp-seg QC JSON as a filename caption + metric table.

    Shared by ``seg_qc_section`` and ``registration_qc_section`` so the
    warp-seg QC table is built in exactly one place.
    """
    parts = [
        f"<p style='font-size:0.85rem;color:#666;margin:8px 0 4px;'>{Path(json_path).name}</p>"
    ]
    try:
        rows = parse_seg_qc_json(json_path)
    except Exception as exc:  # noqa: BLE001 - report, never crash
        parts.append(f'<p class="empty-notice">Could not parse {Path(json_path).name}: {exc}</p>')
        return "\n".join(parts)
    tbl = "<table><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>"
    for k, v in rows:
        tbl += f"<tr><td>{k}</td><td>{v}</td></tr>"
    tbl += "</tbody></table>"
    parts.append(tbl)
    return "\n".join(parts)


def seg_qc_section(seg_qc_dir):
    """Render warp-seg QC JSONs (one table per file)."""
    jsons = list_files(seg_qc_dir, "*.json")
    if not jsons:
        body = '<p class="empty-notice">No warp-segmentation QC metrics found.</p>'
        return section("Segmentation Warp QC", body)
    parts = [_seg_qc_table(jp) for jp in jsons]
    return section("Segmentation Warp QC", "\n".join(parts))


def seg_overlay_section(postprocess_dir):
    """Dedicated section for the *_seg_overlay* PNGs (the most-inspected QC)."""
    all_pngs = list_files(postprocess_dir, "*.png")
    overlays = [p for p in all_pngs if "_seg_overlay" in p.name]
    return section("Segmentation Overlays", img_grid(overlays, wide=True))


def seg_eval_section(seg_eval_dir):
    """
    Render the Cell Segmentation Evaluator (CSE) metrics table.
    Any number of segmentation_metrics.csv files may be present (one per
    merge, though normally just one); each is rendered as its own table
    since column sets can differ across runs.
    """
    parts = []
    csv_files = list_files(seg_eval_dir, "*.csv")
    if not csv_files:
        parts.append(
            '<p class="empty-notice">No segmentation-evaluation CSVs found.</p>'
        )
    else:
        for csv_path in csv_files:
            try:
                headers, rows = parse_seg_eval_csv(csv_path)
            except Exception as exc:
                parts.append(
                    f'<p class="empty-notice">Could not parse {Path(csv_path).name}: {exc}</p>'
                )
                continue
            parts.append(
                f"<p style='font-size:0.85rem;color:#666;margin-bottom:6px;'>{Path(csv_path).name}</p>"
            )
            tbl = (
                "<table><thead><tr>"
                + "".join(f"<th>{h}</th>" for h in headers)
                + "</tr></thead><tbody>"
            )
            for row in rows:
                tbl += "<tr>" + "".join(f"<td>{v}</td>" for v in row) + "</tr>"
            tbl += "</tbody></table>"
            parts.append(tbl)
    return section("Segmentation Quality (CSE)", "\n".join(parts))


# ---------------------------------------------------------------------------
# Data copy
# ---------------------------------------------------------------------------


def copy_data(args):
    """Copy the raw QC artifacts (PNGs, CSVs) into the report's data directory."""
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    def copy_glob(src_dir, pattern, subdir):
        """Copy every file matching ``pattern`` in ``src_dir`` into ``data_dir/subdir``."""
        files = list_files(src_dir, pattern)
        if not files:
            return
        dest = data_dir / subdir
        dest.mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.copy2(f, dest / Path(f).name)

    copy_glob(args.preprocess_qc, "*.png", "preprocess_qc")
    copy_glob(args.registration_qc, "*.png", "registration_qc")
    copy_glob(args.feature_distances, "*.json", "feature_dist")
    copy_glob(args.valis_summary, "*.csv", "valis_summary")
    copy_glob(args.postprocess_qc, "*.png", "postprocess_qc")
    copy_glob(args.seg_eval, "*.csv", "seg_eval")
    copy_glob(args.distance_plots, "*.png", "distance_plots")
    copy_glob(args.seg_qc, "*.json", "seg_qc")
    if args.run_summary and Path(args.run_summary).exists():
        shutil.copy2(args.run_summary, data_dir / "run_summary.json")
    if args.versions and Path(args.versions).exists():
        shutil.copy2(args.versions, data_dir / "collated_versions.yml")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    """CLI entry point: assemble the per-step QC outputs into a single HTML report."""
    configure_logging(level=logging.INFO)
    args = parse_args()
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    summary = parse_run_summary_json(args.run_summary)
    present = {
        "Preprocessing": bool(list_files(args.preprocess_qc, "*.png")),
        "Registration": bool(list_files(args.registration_qc, "*")),
        "Segmentation & Quant": bool(list_files(args.postprocess_qc, "*.png"))
        or bool(list_files(args.seg_eval, "*.csv")),
    }

    html_parts = [html_header(timestamp)]
    html_parts.append(run_summary_section(summary))
    html_parts.append(status_strip_section(present))
    html_parts.append(manifest_section(summary))
    html_parts.append(preprocess_qc_section(args.preprocess_qc))
    html_parts.append(
        registration_qc_section(
            args.registration_qc,
            args.feature_distances,
            args.valis_summary,
            args.distance_plots,
            args.seg_qc,
        )
    )
    html_parts.append(seg_overlay_section(args.postprocess_qc))
    html_parts.append(postprocess_qc_section(args.postprocess_qc))
    html_parts.append(seg_eval_section(args.seg_eval))
    html_parts.append(versions_section(args.versions))
    html_parts.append(html_footer())

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("".join(html_parts))

    logger.info(f"Report written to: {out_path}")

    copy_data(args)
    logger.info(f"Data copied to: {args.data_dir}")


if __name__ == "__main__":
    main()
