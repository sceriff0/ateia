"""Rank variants, produce a recommendation, render a self-contained HTML report."""
from __future__ import annotations
import html


def rank_variants(results):
    ranked = []
    for r in results:
        m = r["metrics"]
        sb = (m["seam_x_before"] + m["seam_y_before"]) / 2
        sa = (m["seam_x_after"] + m["seam_y_after"]) / 2
        seam_gain = 1.0 - (sa / sb) if sb > 0 else 0.0
        cv_gain = 1.0 - (m["cv_after"] / m["cv_before"]) if m["cv_before"] > 0 else 0.0
        ranked.append({
            "name": r["name"], "seam_gain": seam_gain, "cv_gain": cv_gain,
            "composite": seam_gain + cv_gain,
            "seconds": m["seconds"], "peak_rss_mb": m["peak_rss_mb"],
        })
    ranked.sort(key=lambda d: d["composite"], reverse=True)
    return ranked


def recommendation_text(ranked):
    if not ranked:
        return "No variants produced."
    win = ranked[0]
    lines = [f"Recommended: **{win['name']}** — highest composite score "
             f"({win['composite']:.3f}): seam suppression {win['seam_gain']*100:.1f}%, "
             f"background-flatness gain {win['cv_gain']*100:.1f}%, "
             f"{win['seconds']:.1f}s, {win['peak_rss_mb']:.0f} MB peak."]
    if len(ranked) > 1:
        r = ranked[1]
        faster = r["seconds"] < win["seconds"]
        lines.append(
            f"Runner-up **{r['name']}** ({r['composite']:.3f})"
            + (f" is faster ({r['seconds']:.1f}s vs {win['seconds']:.1f}s); "
               "prefer it if the composite gap is within visual noise and runtime matters."
               if faster else
               "; the winner leads on both quality and is not slower, so prefer the winner."))
    lines.append("When scores are within ~0.02 of each other, treat them as a tie and "
                 "decide from the before/after crops and QuPath pyramids — the metrics "
                 "cannot see local artifacts the eye catches.")
    return "\n\n".join(lines)


def write_report(results, plot_index, out_html):
    ranked = rank_variants(results)
    rows = "".join(
        f"<tr><td>{i+1}</td><td>{html.escape(d['name'])}</td>"
        f"<td>{d['composite']:.3f}</td><td>{d['seam_gain']*100:.1f}%</td>"
        f"<td>{d['cv_gain']*100:.1f}%</td><td>{d['seconds']:.1f}</td>"
        f"<td>{d['peak_rss_mb']:.0f}</td></tr>"
        for i, d in enumerate(ranked))
    plots_html = ""
    for name, paths in plot_index.items():
        imgs = "".join(f'<div><img src="{html.escape(p)}" style="max-width:100%"></div>'
                       for p in paths)
        plots_html += f"<h3>{html.escape(name)}</h3>{imgs}"
    rec = recommendation_text(ranked).replace("\n\n", "<br><br>").replace("**", "")
    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Illumination-correction benchmark</title>
<style>body{{font-family:system-ui,sans-serif;margin:2rem;max-width:1100px}}
table{{border-collapse:collapse}}td,th{{border:1px solid #ccc;padding:4px 8px}}
.rec{{background:#f4f7ff;border-left:4px solid #47c;padding:1rem;margin:1rem 0}}</style>
</head><body>
<h1>Illumination-correction benchmark</h1>
<div class="rec">{rec}</div>
<h2>Leaderboard</h2>
<table><tr><th>#</th><th>Variant</th><th>Composite</th><th>Seam gain</th>
<th>CV gain</th><th>Seconds</th><th>Peak MB</th></tr>{rows}</table>
<h2>Diagnostics</h2>{plots_html}
</body></html>"""
    open(out_html, "w").write(doc)
    return str(out_html)
