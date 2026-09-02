#!/usr/bin/env python3
"""
generate_qc_report.py
Generates a self-contained HTML QC report for a MIRAGE pipeline run.
All images are embedded as base64; no external dependencies required.
"""

import argparse
import base64
import csv
import html
import itertools
import json
import logging
import shutil
import sys
from datetime import datetime, timezone
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
        "--registration-tre",
        default="registration_tre/",
        help=(
            "Directory of the registration method's own target-registration-error "
            "artifacts (VALIS *_summary.csv / STARE *_tre.json)"
        ),
    )
    p.add_argument(
        "--postprocess-qc",
        default="postprocess_qc/",
        help="Directory of postprocessing QC PNGs",
    )
    p.add_argument("--versions", default=None, help="Path to collated versions.yml")
    p.add_argument("--run-summary", default=None, help="Path to run_summary.json")
    p.add_argument(
        "--seg-qc",
        default="seg_qc/",
        help="Directory of warp-segmentation QC JSONs",
    )
    p.add_argument(
        "--seg-residuals",
        default="seg_residuals/",
        help="Directory of per-cell registration residual CSVs (SEG_QC.out.per_cell)",
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


def parse_csv_table(csv_path):
    """
    Parse a generic CSV into (headers, rows) where each row is a list of
    strings in header order. Used for the Valis preprocessed_summary.csv
    (rTRE, D, n_matches, etc.); kept generic (columns are whatever the CSV
    declares) so any future summary CSV can reuse it unchanged.
    """
    with open(csv_path, newline="") as fh:
        reader = csv.DictReader(fh)
        display_headers = reader.fieldnames or []
        rows = []
        for row in reader:
            rows.append([row.get(h, "") for h in display_headers])
    return display_headers, rows


def parse_csv_table_head(csv_path, limit):
    """Parse only the first ``limit`` rows, and count the rest without building them.

    Returns ``(headers, rows, total)``. The per-cell residual CSVs are one row per cell, and the
    QC report shows 500 of them -- materialising the other few hundred thousand to compute a
    length is the whole cost. Measured 2.53x / -248 MB (PERF-PLAN C16).

    ``itertools.islice``, NOT ``zip(reader, range(limit))``: zip pulls one item from the reader
    that it never yields, so the tail count comes out one short. Measured on a 400 000-row file,
    the zip form reported 399 999. Pinned by the boundary case in
    tests/test_no_full_mask_unique.py.
    """
    with open(csv_path, newline="") as fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        rows = [
            [row.get(h, "") for h in headers] for row in itertools.islice(reader, limit)
        ]
        total = len(rows) + sum(1 for _ in reader)
    return headers, rows, total


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
        proc_esc = html.escape(str(proc))
        for i, tool in enumerate(sorted(tools)):
            proc_cell = f"<td rowspan='{len(tools)}'>{proc_esc}</td>" if i == 0 else ""
            tbl += (
                f"<tr>{proc_cell}<td>{html.escape(str(tool))}</td>"
                f"<td>{html.escape(str(tools[tool]))}</td></tr>"
            )
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
        f"<tr><th style='width:240px'>{html.escape(str(k))}</th>"
        f"<td>{'' if v is None else html.escape(str(v))}</td></tr>"
        for k, v in pairs
    )
    return f"<table><tbody>{rows}</tbody></table>"


def run_summary_section(summary):
    """Top overview card: pipeline, run context, and key parameters used."""
    if not summary:
        return section(
            "Run Summary", '<p class="empty-notice">Run summary not available.</p>'
        )
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
            f"{html.escape(str(stage))}: {label}</span>"
        )
    return section("Pipeline Stages", "<div>" + "".join(badges) + "</div>")


def manifest_section(summary):
    """Sample manifest: totals plus a per-patient image/channel table."""
    manifest = (summary or {}).get("manifest", {})
    if not manifest:
        return section(
            "Sample Manifest", '<p class="empty-notice">Manifest not available.</p>'
        )
    totals = manifest.get("totals", {})
    patients = manifest.get("patients", {})
    head = _kv_table(
        [
            ("Patients", totals.get("patients")),
            ("Images", totals.get("images")),
            ("Channels", totals.get("channels")),
        ]
    )
    tbl = (
        "<table style='margin-top:14px'><thead><tr>"
        "<th>Patient</th><th>Images</th><th>Channels</th>"
        "</tr></thead><tbody>"
    )
    for pid in sorted(patients):
        row = patients[pid]
        tbl += (
            f"<tr><td>{html.escape(str(pid))}</td>"
            f"<td>{html.escape(str(row.get('images', '')))}</td>"
            f"<td>{html.escape(str(row.get('channels', '')))}</td></tr>"
        )
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


def _html_table(headers, rows, extra_body_html=""):
    """
    Build a complete ``<table>`` (with ``<thead>``/``<tbody>``), escaping
    every header and every cell value via ``html.escape``.

    Centralizes the table-render logic that used to be duplicated across the
    registration-QC Valis rTRE table and the STARE tiled-TRE table, and
    ensures neither can inject raw CSV/JSON-derived HTML into the report.

    `extra_body_html`: optional pre-rendered ``<tr>...</tr>`` HTML appended
    after the normal rows — for full-width/``colspan`` rows (e.g. a
    per-file parse-error notice) that don't fit the uniform one-cell-per-
    header row shape every other row uses. The caller is responsible for
    escaping any dynamic values it interpolates into this HTML itself.
    """
    thead = "".join(f"<th>{html.escape(str(h))}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in row) + "</tr>"
        for row in rows
    )
    body += extra_body_html
    return f"<table><thead><tr>{thead}</tr></thead><tbody>{body}</tbody></table>"


def img_grid(png_files, wide=False):
    """Render a list of PNG files as a grid of base64-embedded image cards."""
    if not png_files:
        return '<p class="empty-notice">No images found.</p>'
    grid_class = "img-grid-wide" if wide else "img-grid"
    cards = []
    for p in png_files:
        b64 = img_to_b64(p)
        name = html.escape(Path(p).name)
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


def registration_qc_section(reg_dir, valis_dir, seg_qc_dir=None):
    """Build the registration-QC report section (overlay images plus accuracy tables)."""
    parts = []

    # Image grid
    pngs = list_files(reg_dir, "*.png")
    parts.append(
        "<h3 style='margin-bottom:8px;font-size:1rem;color:#444;'>Overlay Images</h3>"
    )
    parts.append(img_grid(pngs))

    # Registration-accuracy summaries. The method decides the format: VALIS emits rTRE CSVs, the
    # tiled ('STARE') method emits its own intrinsic-TRE JSON (_tre.json) — both land here.
    valis_csvs = list_files(valis_dir, "*.csv")
    tiled_tre_jsons = list_files(valis_dir, "*_tre.json")
    if valis_csvs:
        parts.append(
            "<h3 style='margin:20px 0 8px;font-size:1rem;color:#444;'>Registration Accuracy (Valis rTRE)</h3>"
        )
        for csv_path in valis_csvs:
            try:
                headers, rows = parse_csv_table(csv_path)
            except Exception as exc:
                parts.append(
                    '<p class="empty-notice">Could not parse '
                    f"{html.escape(Path(csv_path).name)}: {html.escape(str(exc))}</p>"
                )
                continue
            parts.append(
                "<p style='font-size:0.85rem;color:#666;margin-bottom:6px;'>"
                f"{html.escape(Path(csv_path).name)}</p>"
            )
            parts.append(_html_table(headers, rows))
    if tiled_tre_jsons:
        parts.append(
            "<h3 style='margin:20px 0 8px;font-size:1rem;color:#444;'>Registration Accuracy "
            "(STARE Tiled TRE)</h3>"
        )
        parts.append(_tiled_tre_tables(tiled_tre_jsons))
    if not valis_csvs and not tiled_tre_jsons:
        parts.append(
            '<p class="empty-notice" style="margin-top:12px;">No registration-accuracy summary found.</p>'
        )

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
        "<p style='font-size:0.85rem;color:#666;margin:8px 0 4px;'>"
        f"{html.escape(Path(json_path).name)}</p>"
    ]
    try:
        rows = parse_seg_qc_json(json_path)
    except Exception as exc:  # noqa: BLE001 - report, never crash
        parts.append(
            '<p class="empty-notice">Could not parse '
            f"{html.escape(Path(json_path).name)}: {html.escape(str(exc))}</p>"
        )
        return "\n".join(parts)
    parts.append(_html_table(["Metric", "Value"], rows))
    return "\n".join(parts)


# ── STARE tiled-registration intrinsic TRE ──────────────────────────────────────
# The tiled ('STARE') method emits its own intrinsic Target Registration Error per slide
# (_tre.json), the way VALIS emits rTRE: coarse (rigid) feature-fit TRE, a per-tile spatial
# heatmap VALIS does not have, and — in the default path — the post-refinement residual (its
# analogue of VALIS's non-rigid error). Rendered here alongside the other registration-accuracy
# tables so both methods surface a comparable number.
def _fmt_px(v):
    return f"{v:.2f}" if isinstance(v, (int, float)) else "n/a"


def parse_tiled_tre_json(path):
    """Flatten a STARE _tre.json into the headline registration-accuracy fields."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)

    def pct(block, key):
        b = data.get(block)
        return b.get(key) if isinstance(b, dict) else None

    tiles = data.get("tiles", []) or []
    # Prefer the counts the solver wrote; fall back to counting the heatmap so a _tre.json
    # from before those keys existed still reports sensibly (missing `accepted` = accepted).
    n_rejected = data.get("n_rejected")
    if n_rejected is None:
        n_rejected = sum(1 for t in tiles if not t.get("accepted", True))
    n_accepted = data.get("n_accepted")
    if n_accepted is None:
        n_accepted = len(tiles) - n_rejected

    return {
        "file": Path(path).name,
        "moving": data.get("moving") or Path(path).name,
        "coarse_tre_px": data.get("coarse_tre_px"),
        "n_tiles": data.get("n_tiles"),
        "n_accepted": n_accepted,
        "n_rejected": n_rejected,
        "mesh_refined": bool(data.get("mesh_refined")),
        "rigid_p50": pct("rigid_tre_px", "p50"),
        "rigid_p90": pct("rigid_tre_px", "p90"),
        "final_p50": pct("residual_after_px", "p50"),
        "final_p90": pct("residual_after_px", "p90"),
        "tiles": tiles,
    }


def _tiled_tre_heatmap_svg(info, cell=14):
    """A compact per-tile TRE heatmap (green=low, red=high) — STARE's spatial accuracy view."""
    tiles = info["tiles"]
    if not tiles:
        return ""
    have_final = all("tre_after" in t for t in tiles)

    def val(t):
        return t["tre_after"] if have_final else t.get("tre_rigid", 0.0)

    def kept(t):
        return bool(t.get("accepted", True))

    # Scale over ACCEPTED tiles only. A rejected tile's value is an artefact of correlating
    # against background — one of them at 131.5px would otherwise set vmax and squash every
    # real tile into the green end, hiding the misalignment the heatmap exists to show.
    vmax = max((val(t) for t in tiles if kept(t)), default=0.0) or 1.0
    nx = max(t["ix"] for t in tiles) + 1
    ny = max(t["iy"] for t in tiles) + 1
    rects = []
    for t in tiles:
        if kept(t):
            frac = min(val(t) / vmax, 1.0)
            r, g, b = int(60 + 195 * frac), int(180 * (1 - frac)), 70
            title = f"tile ({t['ix']},{t['iy']}): {val(t):.2f}px"
        else:
            # Neutral grey, never on the green–red scale: this tile carries no measurement.
            r, g, b = 200, 200, 200
            title = f"tile ({t['ix']},{t['iy']}): rejected — not in the mesh"
        rects.append(
            f'<rect x="{t["ix"] * cell}" y="{t["iy"] * cell}" width="{cell - 1}" '
            f'height="{cell - 1}" fill="rgb({r},{g},{b})">'
            f"<title>{title}</title></rect>"
        )
    label = "post-refinement residual" if have_final else "rigid-stage TRE"
    n_rejected = sum(1 for t in tiles if not kept(t))
    dropped = f"; {n_rejected} rejected (grey)" if n_rejected else ""
    return (
        '<div style="margin:8px 0 4px;">'
        f'<p style="font-size:0.8rem;color:#666;margin:0 0 2px;">'
        f"{html.escape(info['moving'])} — per-tile {label} "
        f"(green=low, red=high; max {vmax:.2f}px{dropped})</p>"
        f'<svg width="{nx * cell}" height="{ny * cell}" '
        f'style="border:1px solid #eee;">{"".join(rects)}</svg></div>'
    )


def _tiled_tre_tables(tre_jsons):
    """Per-slide STARE-TRE summary table + spatial heatmaps."""
    headers = [
        "Slide",
        "Coarse TRE (px)",
        "Rigid p50 / p90 (px)",
        "Final p50 / p90 (px)",
        "Refined",
        "Tiles",
    ]
    rows, heatmaps, err_html = [], [], []
    for jp in sorted(tre_jsons):
        try:
            info = parse_tiled_tre_json(jp)
        except Exception as exc:  # noqa: BLE001 - report, never crash
            err_html.append(
                f"<tr><td>{html.escape(Path(jp).name)}</td>"
                f"<td colspan='5'>Parse error: {html.escape(str(exc))}</td></tr>"
            )
            continue
        final = (
            f"{_fmt_px(info['final_p50'])} / {_fmt_px(info['final_p90'])}"
            if info["final_p50"] is not None
            else "n/a (fan-out — see benchmark)"
        )
        rows.append(
            [
                info["moving"],
                _fmt_px(info["coarse_tre_px"]),
                f"{_fmt_px(info['rigid_p50'])} / {_fmt_px(info['rigid_p90'])}",
                final,
                "yes" if info["mesh_refined"] else "no",
                # "400" hides that 60 of them never reached the mesh; show the ratio only
                # when there is something to show.
                f"{info['n_accepted']} / {info['n_tiles']}"
                if info["n_rejected"]
                else str(info["n_tiles"]),
            ]
        )
        hm = _tiled_tre_heatmap_svg(info)
        if hm:
            heatmaps.append(hm)
    parts = [_html_table(headers, rows, "".join(err_html))]
    parts.extend(heatmaps)
    return "\n".join(parts)


# ── (C) feature-TRE vs cell-displacement reconciliation ─────────────────────────
# VALIS scores registration on its own SuperPoint/SuperGlue keypoints — a self-referential,
# optimistic feature-TRE — while WARP_SEG_QC scores it on independently segmented cells. Lining
# the two up per stage exposes the failure mode neither catches alone: features declaring a slide
# aligned while the cells say otherwise. A stage counts as divergent when the two disagree by more
# than this factor; below it they are treated as corroborating.
RECONCILE_DIVERGENCE_RATIO = 3.0

# Each stage's feature-TRE comes from a specific VALIS summary column, because register_micro()
# composes the micro residual into the same field and rewrites non_rigid_D in place: only the
# pre-micro summary still isolates the non-rigid stage. (col, which_summary).
_RECONCILE_TRE_SOURCE = {
    "rigid": ("rigid_D", "final"),
    "non_rigid": ("non_rigid_D", "premicro"),
    "micro": ("non_rigid_D", "final"),
}


def _to_float(v):
    try:
        f = float(v)
        return f if f == f else None  # drop NaN
    except (TypeError, ValueError):
        return None


def _read_intrinsic_tre(tre_dir):
    """slide -> {'final': {col: val}, 'premicro': {col: val}} of intrinsic TRE, either method's shape.

    VALIS emits per-patient ``*_summary[.csv]`` rows (and a ``_premicro`` variant) keyed by the
    ``from``/``filename`` column: read unchanged into ``final``/``premicro``.

    STARE (tiled) emits one per-slide ``*_tre.json``, keyed by its ``moving`` field. Per
    ``bin/utils/tre_report.py``'s module docstring, ``coarse_tre_px`` is the rigid-stage TRE
    ("directly comparable to VALIS's rigid error") and ``residual_after_px`` is the
    post-registration number ("the analogue of VALIS's non-rigid error"); its p50 is used so it
    lines up with the seg-QC side's ``displacement_*_p50``. STARE has no separate pre-micro stage,
    so both values land in ``final`` — the existing non-rigid fallback in ``reconcile_rows``
    (final's ``non_rigid_D`` when no premicro summary exists) already covers it. Both formats
    write into the same slide dict so a directory containing both is a merge, not a shadow.
    """
    out = {}
    for csv_path in list_files(tre_dir, "*.csv"):
        which = (
            "premicro" if csv_path.name.endswith("_summary_premicro.csv") else "final"
        )
        try:
            with open(csv_path, newline="") as fh:
                for row in csv.DictReader(fh):
                    slide = row.get("from") or row.get("filename")
                    if not slide:
                        continue
                    slot = out.setdefault(str(slide), {"final": {}, "premicro": {}})[
                        which
                    ]
                    for col in ("rigid_D", "non_rigid_D"):
                        if col in row:
                            slot[col] = _to_float(row[col])
        except (OSError, csv.Error):
            continue
    for json_path in list_files(tre_dir, "*_tre.json"):
        try:
            info = parse_tiled_tre_json(json_path)
        except (OSError, ValueError):
            continue
        slide = info["moving"]
        slot = out.setdefault(str(slide), {"final": {}, "premicro": {}})["final"]
        slot["rigid_D"] = _to_float(info["coarse_tre_px"])
        slot["non_rigid_D"] = _to_float(info["final_p50"])
    return out


def _read_seg_cell_disp(seg_qc_dir):
    """slide -> {stage: displacement_µm_p50} from the WARP_SEG_QC JSONs (px if no µm present)."""
    out = {}
    for jp in list_files(seg_qc_dir, "*.json"):
        try:
            with open(jp, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        slide = data.get("moving")
        stages = data.get("stages") or {}
        if not slide or not isinstance(stages, dict):
            continue
        per_stage = {}
        for stage, metrics in stages.items():
            if not isinstance(metrics, dict):
                continue
            val = metrics.get("displacement_um_p50")
            if val is None:
                val = metrics.get(
                    "displacement_px_p50"
                )  # fall back to px if no pixel size
            per_stage[stage] = _to_float(val)
        out[str(slide)] = per_stage
    return out


def reconcile_rows(tre_dir, seg_qc_dir):
    """Pair the registration method's intrinsic TRE with WARP_SEG_QC cell displacement, per slide per stage.

    Returns a list of dicts: ``slide``, ``stage``, ``feature_tre_um``, ``cell_disp_um``,
    ``divergent`` (True/False, or None when either number is missing so no verdict is possible).
    Slides are keyed by slide name (VALIS's ``from``/``filename`` or STARE's ``moving`` — both
    resolve to the same identifier); rows are only emitted for slides that appear in the seg-QC
    output.
    """
    tre = _read_intrinsic_tre(tre_dir)
    cell = _read_seg_cell_disp(seg_qc_dir)
    rows = []
    for slide in sorted(cell):
        slide_tre = tre.get(slide, {})
        for stage in ("rigid", "non_rigid", "micro"):
            col, which = _RECONCILE_TRE_SOURCE[stage]
            feature = slide_tre.get(which, {}).get(col)
            # non_rigid_D is overwritten in place by register_micro(), so only the pre-micro
            # summary isolates the non-rigid TRE. But when no pre-micro summary exists
            # (micro_reg < 2 -> register_micro never ran), the final summary still IS the
            # pre-micro non-rigid value: fall back to it. Without this the non_rigid row is
            # permanently n/a at the shipped default (micro_reg=0), defeating the whole
            # reconciliation for the configuration that actually runs.
            if feature is None and stage == "non_rigid" and which == "premicro":
                feature = slide_tre.get("final", {}).get(col)
            disp = cell.get(slide, {}).get(stage)
            divergent = None
            if feature is not None and disp is not None:
                lo, hi = sorted((abs(feature), abs(disp)))
                divergent = hi > RECONCILE_DIVERGENCE_RATIO * lo if lo > 0 else hi > 0
            rows.append(
                {
                    "slide": slide,
                    "stage": stage,
                    "feature_tre_um": feature,
                    "cell_disp_um": disp,
                    "divergent": divergent,
                }
            )
    return rows


def reconciliation_section(tre_dir, seg_qc_dir):
    """Render the feature-TRE vs cell-displacement reconciliation table."""
    title = "Feature-TRE vs Cell-Displacement Reconciliation"
    rows = reconcile_rows(tre_dir, seg_qc_dir)
    if not rows:
        return section(
            title,
            '<p class="empty-notice">No overlapping registration TRE + segmentation-warp QC '
            "found to reconcile.</p>",
        )

    def fmt(v):
        return f"{v:.3f}" if isinstance(v, float) else "—"

    body = [
        "<p style='font-size:0.85rem;color:#666;margin:0 0 8px;'>"
        "VALIS feature-TRE is measured on its own SuperPoint/SuperGlue keypoints (optimistic); "
        "cell displacement is measured on independently segmented nuclei. A flagged row means "
        f"the two disagree by more than {RECONCILE_DIVERGENCE_RATIO:g}× — worth a look.</p>",
        "<table><thead><tr><th>Slide</th><th>Stage</th><th>Feature-TRE (µm)</th>"
        "<th>Cell disp. p50 (µm)</th><th>Agreement</th></tr></thead><tbody>",
    ]
    for r in rows:
        if r["divergent"] is None:
            flag = "<span style='color:#999;'>n/a</span>"
        elif r["divergent"]:
            flag = "<span style='color:#c0392b;font-weight:600;'>⚠ divergent</span>"
        else:
            flag = "<span style='color:#27ae60;'>✓ agree</span>"
        body.append(
            f"<tr><td>{r['slide']}</td><td>{r['stage']}</td>"
            f"<td>{fmt(r['feature_tre_um'])}</td><td>{fmt(r['cell_disp_um'])}</td>"
            f"<td>{flag}</td></tr>"
        )
    body.append("</tbody></table>")
    return section(title, "\n".join(body))


def seg_qc_section(seg_qc_dir):
    """Render warp-seg QC JSONs (one table per file)."""
    jsons = list_files(seg_qc_dir, "*.json")
    if not jsons:
        body = '<p class="empty-notice">No warp-segmentation QC metrics found.</p>'
        return section("Segmentation Warp QC", body)
    parts = [_seg_qc_table(jp) for jp in jsons]
    return section("Segmentation Warp QC", "\n".join(parts))


# bin/warp_seg_qc.py's write_per_cell_csv writes one row per matched cell pair,
# uncapped -- a real slide can be 10^4-10^6 rows. Every OTHER CSV/JSON table in this
# report is per-slide or per-stage (a few dozen rows at most); inlining every residual
# row into one HTML file would produce a browser-killing document the 535-byte stub
# fixtures never surface. Cap what is rendered and say so, rather than either silently
# truncating or dumping the whole thing.
SEG_RESIDUALS_MAX_ROWS = 500


def seg_residuals_section(seg_residuals_dir):
    """Render per-cell registration-residual CSVs (one table per file, row-capped)."""
    csvs = list_files(seg_residuals_dir, "*.csv")
    if not csvs:
        body = '<p class="empty-notice">No per-cell registration residuals found.</p>'
        return section("Per-Cell Registration Residuals", body)
    parts = []
    for csv_path in csvs:
        parts.append(
            "<p style='font-size:0.85rem;color:#666;margin:8px 0 4px;'>"
            f"{html.escape(Path(csv_path).name)}</p>"
        )
        try:
            headers, shown, total = parse_csv_table_head(
                csv_path, SEG_RESIDUALS_MAX_ROWS
            )
        except Exception as exc:  # noqa: BLE001 - report, never crash
            parts.append(
                '<p class="empty-notice">Could not parse '
                f"{html.escape(Path(csv_path).name)}: {html.escape(str(exc))}</p>"
            )
            continue
        if total > len(shown):
            parts.append(
                "<p style='font-size:0.8rem;color:#888;margin:0 0 6px;'>"
                f"Showing {len(shown)} of {total} rows.</p>"
            )
        parts.append(_html_table(headers, shown))
    return section("Per-Cell Registration Residuals", "\n".join(parts))


def seg_overlay_section(postprocess_dir):
    """Dedicated section for the *_seg_overlay* PNGs (the most-inspected QC)."""
    all_pngs = list_files(postprocess_dir, "*.png")
    overlays = [p for p in all_pngs if "_seg_overlay" in p.name]
    return section("Segmentation Overlays", img_grid(overlays, wide=True))


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
    copy_glob(args.registration_tre, "*.csv", "registration_tre")
    copy_glob(args.registration_tre, "*_tre.json", "registration_tre")
    copy_glob(args.postprocess_qc, "*.png", "postprocess_qc")
    copy_glob(args.seg_qc, "*.json", "seg_qc")
    copy_glob(args.seg_residuals, "*.csv", "seg_residuals")
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
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    summary = parse_run_summary_json(args.run_summary)
    present = {
        "Preprocessing": bool(list_files(args.preprocess_qc, "*.png")),
        "Registration": bool(list_files(args.registration_qc, "*")),
        "Segmentation & Quant": bool(list_files(args.postprocess_qc, "*.png")),
    }

    html_parts = [html_header(timestamp)]
    html_parts.append(run_summary_section(summary))
    html_parts.append(status_strip_section(present))
    html_parts.append(manifest_section(summary))
    html_parts.append(preprocess_qc_section(args.preprocess_qc))
    html_parts.append(
        registration_qc_section(
            args.registration_qc,
            args.registration_tre,
            args.seg_qc,
        )
    )
    html_parts.append(reconciliation_section(args.registration_tre, args.seg_qc))
    html_parts.append(seg_residuals_section(args.seg_residuals))
    html_parts.append(seg_overlay_section(args.postprocess_qc))
    html_parts.append(postprocess_qc_section(args.postprocess_qc))
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
