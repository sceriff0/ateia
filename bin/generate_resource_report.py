#!/usr/bin/env python3
"""
generate_resource_report.py
Self-contained HTML computational-resources report for a MIRAGE run.
Joins the aggregated input-size log with Nextflow's trace.txt. Stdlib only.
"""

import argparse
import csv
import html
import re
from datetime import datetime, timezone
from pathlib import Path

_UNIT = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}


def parse_bytes(s):
    """Parse a Nextflow byte string ('3.2 GB', '512 MB', '-') to float bytes."""
    if s is None:
        return None
    s = s.strip()
    # Operator precedence note: `and` binds tighter than `or`, so the buggy
    # source form of this condition (`s in {"-", "0"} and s == "-"`) reduces,
    # for every value of s, to exactly `s == "-"` — the "0" set member was
    # inert and never fired. A bare "0" is a real zero-byte reading (e.g.
    # peak_rss/rchar/wchar can genuinely be 0 for a trivial task) and must
    # keep falling through to float("0") == 0.0, matching parse_duration's
    # and parse_percent's identical `not s or s == "-"` guard. Only "-"
    # (Nextflow's not-run/cached sentinel) and empty are "missing".
    if not s or s == "-":
        return None
    m = re.match(r"^([\d.]+)\s*([KMGT]?B)$", s, re.IGNORECASE)
    if m:
        return round(float(m.group(1)) * _UNIT[m.group(2).upper()], 1)
    try:
        return float(s)
    except ValueError:
        return None


def parse_duration(s):
    """Parse a duration ('12m 4s', '2h 1m', '1.5s', '-') to float seconds."""
    if s is None:
        return None
    s = s.strip()
    if not s or s == "-":
        return None
    total = 0.0
    found = False
    for value, unit in re.findall(r"([\d.]+)\s*(ms|s|m|h|d)", s):
        found = True
        v = float(value)
        total += {"ms": v / 1000, "s": v, "m": v * 60, "h": v * 3600, "d": v * 86400}[
            unit
        ]
    if found:
        return total
    try:
        return float(s)
    except ValueError:
        return None


def parse_percent(s):
    """Parse a percentage string ('142.3%', '-') to float."""
    if s is None:
        return None
    s = s.strip().rstrip("%")
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_trace(path):
    """Parse trace.txt (TSV) into a list of per-task dicts with normalized fields."""
    rows = []
    if not path or not Path(path).exists():
        return rows
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        for r in reader:
            rows.append(
                {
                    "process": (r.get("process") or "").strip(),
                    "tag": (r.get("tag") or "").strip(),
                    "name": (r.get("name") or "").strip(),
                    "status": (r.get("status") or "").strip(),
                    "exit": (r.get("exit") or "").strip(),
                    "realtime_s": parse_duration(r.get("realtime")),
                    "duration_s": parse_duration(r.get("duration")),
                    "cpu_pct": parse_percent(r.get("%cpu")),
                    "peak_rss_b": parse_bytes(r.get("peak_rss")),
                    "peak_vmem_b": parse_bytes(r.get("peak_vmem")),
                    "rchar_b": parse_bytes(r.get("rchar")),
                    "wchar_b": parse_bytes(r.get("wchar")),
                }
            )
    return rows


def parse_size_log(path):
    """Sum input bytes per (process, sample_id) from input_sizes.csv."""
    out = {}
    if not path or not Path(path).exists():
        return out
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            proc = (row.get("process") or "").strip()
            sample = (row.get("sample_id") or "").strip()
            if not proc or proc == "STUB":
                continue
            try:
                b = int(float(row.get("bytes") or 0))
            except ValueError:
                b = 0
            out[(proc, sample)] = out.get((proc, sample), 0) + b
    return out


def _maxf(values):
    """Max of the non-None floats, or None."""
    vals = [v for v in values if v is not None]
    return max(vals) if vals else None


def _sumf(values):
    """Sum of the non-None floats (0.0 if all None)."""
    return sum(v for v in values if v is not None)


def rollup_by_process(trace_rows):
    """Aggregate trace rows into per-process resource summaries."""
    groups = {}
    for r in trace_rows:
        groups.setdefault(r["process"], []).append(r)
    out = []
    for proc, rows in sorted(groups.items()):
        rts = [r.get("realtime_s") for r in rows]
        realtime_total = _sumf(rts)
        n = len(rows)
        out.append(
            {
                "process": proc,
                "n_tasks": n,
                "realtime_total_s": realtime_total,
                "realtime_mean_s": round(realtime_total / n, 1) if n else 0.0,
                "cpu_max_pct": _maxf([r.get("cpu_pct") for r in rows]),
                "peak_rss_max_b": _maxf([r.get("peak_rss_b") for r in rows]),
                "peak_vmem_max_b": _maxf([r.get("peak_vmem_b") for r in rows]),
                "rchar_total_b": _sumf([r.get("rchar_b") for r in rows]),
                "wchar_total_b": _sumf([r.get("wchar_b") for r in rows]),
                "n_failed": sum(
                    1 for r in rows if r.get("exit") not in ("0", "", "-", None)
                ),
            }
        )
    return out


def join_size(trace_rows, size_map):
    """Attach input_bytes to each trace row via (process, tag), with prefix fallback."""
    # Pre-index sample ids by process for fallback matching.
    by_proc = {}
    for (proc, sample), b in size_map.items():
        by_proc.setdefault(proc, []).append((sample, b))
    joined = []
    for r in trace_rows:
        proc, tag = r["process"], r.get("tag", "")
        input_bytes = size_map.get((proc, tag))
        if input_bytes is None:
            # Fallback: a size sample whose id is a prefix of the trace tag
            # (trace tag is meta.id = "<patient>_<stem>"; size sample is patient).
            # Boundary-match on "_" so sample "P1" cannot prefix-match tag
            # "P10_slide" (a plain startswith would false-positive there).
            for sample, b in by_proc.get(proc, []):
                if sample and (tag == sample or tag.startswith(sample + "_")):
                    input_bytes = b
                    break
        joined.append({**r, "input_bytes": input_bytes})
    return joined


def fmt_bytes(b):
    if b is None:
        return "N/A"
    b = float(b)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024 or unit == "TB":
            return f"{b:.1f} {unit}"
        b /= 1024


def fmt_secs(s):
    if s is None:
        return "N/A"
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {sec}s"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


_CSS = """
body{font-family:'Segoe UI',Arial,sans-serif;background:#f4f6f9;color:#333;margin:0}
header{background:#1a2332;color:#fff;padding:24px 40px}
main{max-width:1400px;margin:32px auto;padding:0 24px 60px}
section{background:#fff;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.1);margin-bottom:32px;overflow:hidden}
h2{background:#2c3e50;color:#fff;padding:14px 20px;font-size:1.15rem;margin:0}
.body{padding:20px}
table{border-collapse:collapse;width:100%;font-size:.88rem}
th{background:#ecf0f1;text-align:left;padding:8px 12px;border-bottom:2px solid #bdc3c7}
td{padding:7px 12px;border-bottom:1px solid #ecf0f1}
tr:hover td{background:#f8f9fa}
.empty{color:#888;font-style:italic}
.fail{color:#c0392b;font-weight:600}
"""


def _esc(v):
    """Escape a dynamic value (process name, tag, status, exit code, path,
    etc.) for safe inclusion in HTML. Kept as its own tiny helper (rather
    than importing anything from generate_qc_report.py) so this script stays
    stdlib-only and self-contained; `html` is stdlib so this is allowed."""
    return html.escape(str(v))


def _html_table(headers, rows):
    """Build a complete ``<table>``, escaping every header and cell value."""
    thead = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_esc(v)}</td>" for v in row) + "</tr>" for row in rows
    )
    return f"<table><thead><tr>{thead}</tr></thead><tbody>{body}</tbody></table>"


def _section(title, body):
    return f"<section><h2>{title}</h2><div class='body'>{body}</div></section>"


def build_html(
    trace_rows, size_map, timestamp, native_report=None, native_timeline=None
):
    parts = [
        "<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'>",
        f"<title>MIRAGE Resource Report</title><style>{_CSS}</style></head><body>",
        f"<header><h1>MIRAGE Computational Resource Report</h1>"
        f"<div style='opacity:.75;font-size:.9rem'>Generated: {timestamp}</div></header><main>",
    ]

    if not trace_rows:
        parts.append(
            _section(
                "Resource Usage",
                "<p class='empty'>Trace data not available "
                "(run with --enable_trace to collect it).</p>",
            )
        )
        parts.append("</main></body></html>")
        return "".join(parts)

    # Run totals
    total_wall = _sumf([r.get("realtime_s") for r in trace_rows])
    n_fail = sum(1 for r in trace_rows if r.get("exit") not in ("0", "", "-", None))
    peak = _maxf([r.get("peak_rss_b") for r in trace_rows])
    totals = (
        f"<table><tbody>"
        f"<tr><th>Total tasks</th><td>{len(trace_rows)}</td></tr>"
        f"<tr><th>Total CPU wall-time</th><td>{fmt_secs(total_wall)}</td></tr>"
        f"<tr><th>Failed/non-zero exit</th><td>{n_fail}</td></tr>"
        f"<tr><th>Peak single-task RSS</th><td>{fmt_bytes(peak)}</td></tr>"
        f"</tbody></table>"
    )
    parts.append(_section("Run Totals", totals))

    # Per-process rollup
    roll = rollup_by_process(trace_rows)
    tbl = (
        "<table><thead><tr><th>Process</th><th>Tasks</th><th>Total time</th>"
        "<th>Mean time</th><th>Max %CPU</th><th>Max peak RSS</th>"
        "<th>Max peak VMEM</th><th>Read</th><th>Write</th><th>Failed</th>"
        "</tr></thead><tbody>"
    )
    for r in roll:
        cpu_max = "" if r["cpu_max_pct"] is None else f"{r['cpu_max_pct']}%"
        tbl += (
            f"<tr><td>{_esc(r['process'])}</td><td>{r['n_tasks']}</td>"
            f"<td>{fmt_secs(r['realtime_total_s'])}</td>"
            f"<td>{fmt_secs(r['realtime_mean_s'])}</td>"
            f"<td>{cpu_max}</td>"
            f"<td>{fmt_bytes(r['peak_rss_max_b'])}</td>"
            f"<td>{fmt_bytes(r['peak_vmem_max_b'])}</td>"
            f"<td>{fmt_bytes(r['rchar_total_b'])}</td>"
            f"<td>{fmt_bytes(r['wchar_total_b'])}</td>"
            f"<td class='{'fail' if r['n_failed'] else ''}'>{r['n_failed']}</td></tr>"
        )
    tbl += "</tbody></table>"
    parts.append(_section("Per-Process Resource Rollup", tbl))

    # Resource vs input size
    joined = join_size(trace_rows, size_map)
    with_size = [j for j in joined if j.get("input_bytes") is not None]
    if with_size:
        tbl = (
            "<table><thead><tr><th>Process</th><th>Sample (tag)</th>"
            "<th>Input size</th><th>Peak RSS</th><th>Realtime</th>"
            "<th>RSS / input GB</th></tr></thead><tbody>"
        )
        for j in sorted(with_size, key=lambda x: -(x.get("peak_rss_b") or 0)):
            ratio = (
                (j["peak_rss_b"] / j["input_bytes"])
                if (j.get("peak_rss_b") is not None and j["input_bytes"])
                else None
            )
            tbl += (
                f"<tr><td>{_esc(j['process'])}</td><td>{_esc(j.get('tag', ''))}</td>"
                f"<td>{fmt_bytes(j['input_bytes'])}</td>"
                f"<td>{fmt_bytes(j.get('peak_rss_b'))}</td>"
                f"<td>{fmt_secs(j.get('realtime_s'))}</td>"
                f"<td>{'' if ratio is None else f'{ratio:.1f}x'}</td></tr>"
            )
        tbl += "</tbody></table>"
        parts.append(_section("Resource vs Input Size", tbl))
    else:
        parts.append(
            _section(
                "Resource vs Input Size",
                "<p class='empty'>No size logs matched trace tasks.</p>",
            )
        )

    # Top-N heaviest / slowest
    heaviest = sorted(
        [r for r in trace_rows if r.get("peak_rss_b")], key=lambda x: -x["peak_rss_b"]
    )[:10]
    slowest = sorted(
        [r for r in trace_rows if r.get("realtime_s")], key=lambda x: -x["realtime_s"]
    )[:10]

    def _top(rows, valf, fmt):
        return _html_table(
            ["Process", "Sample", "Value"],
            [[r["process"], r.get("tag", ""), fmt(valf(r))] for r in rows],
        )

    parts.append(
        _section(
            "Top 10 by Peak RSS", _top(heaviest, lambda r: r["peak_rss_b"], fmt_bytes)
        )
    )
    parts.append(
        _section(
            "Top 10 by Runtime", _top(slowest, lambda r: r["realtime_s"], fmt_secs)
        )
    )

    # Retries & failures
    fails = [r for r in trace_rows if r.get("exit") not in ("0", "", "-", None)]
    if fails:
        t = "<table><thead><tr><th>Process</th><th>Sample</th><th>Status</th><th>Exit</th></tr></thead><tbody>"
        for r in fails:
            t += (
                f"<tr><td>{_esc(r['process'])}</td><td>{_esc(r.get('tag', ''))}</td>"
                f"<td>{_esc(r.get('status', ''))}</td>"
                f"<td class='fail'>{_esc(r.get('exit', ''))}</td></tr>"
            )
        t += "</tbody></table>"
        parts.append(_section("Retries &amp; Failures", t))
    else:
        parts.append(
            _section(
                "Retries &amp; Failures",
                "<p class='empty'>No failed or non-zero-exit tasks.</p>",
            )
        )

    # Pointers to native reports
    links = []
    if native_report:
        links.append(
            f"<li>Interactive execution report: <code>{_esc(native_report)}</code></li>"
        )
    if native_timeline:
        links.append(f"<li>Timeline: <code>{_esc(native_timeline)}</code></li>")
    if links:
        parts.append(
            _section("Nextflow Native Reports", "<ul>" + "".join(links) + "</ul>")
        )

    parts.append("</main></body></html>")
    return "".join(parts)


def parse_args():
    p = argparse.ArgumentParser(
        description="Generate MIRAGE computational-resource report"
    )
    p.add_argument("--trace", default=".trace/trace.txt")
    p.add_argument("--size-log", default=None)
    p.add_argument("--output", default="mirage_resource_report.html")
    p.add_argument("--native-report", default=None)
    p.add_argument("--native-timeline", default=None)
    return p.parse_args()


def main():
    args = parse_args()
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    trace_rows = parse_trace(args.trace)
    size_map = parse_size_log(args.size_log)
    # Named report_html (not `html`) to avoid shadowing the stdlib `html`
    # module used for escaping elsewhere in this file.
    report_html = build_html(
        trace_rows, size_map, timestamp, args.native_report, args.native_timeline
    )
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report_html, encoding="utf-8")
    print(f"Resource report written to: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
