"""Rank variants, produce a recommendation, render a self-contained HTML report."""
from __future__ import annotations
import html


def _squash(g):
    """Sign-preserving soft bound of a relative gain into (-1, 1).

    Keeps the 'worse than uncorrected' signal (negative) while stopping one
    unbounded term (the old cv_gain hit -6.5) from dominating the ranking.
    """
    return g / (1.0 + abs(g))


def rank_variants(results):
    """Rank variants by an un-gameable composite = artifact_score × fidelity.

    artifact_score in (-1, 1) is the mean of bounded seam- and flatness-gains.
    fidelity in [0, 1] (no-reference signal retention) MULTIPLIES it, so a variant
    that "suppresses seams" by driving the image toward black — high artifact
    score but fidelity → 0 — scores ~0 instead of winning. See
    research/illumination-correction-sota-2026-07-22.md. Entries without a
    `fidelity` metric (legacy/synthetic fixtures) default to a neutral 1.0.
    """
    ranked = []
    for r in results:
        m = r["metrics"]
        sb = (m["seam_x_before"] + m["seam_y_before"]) / 2
        sa = (m["seam_x_after"] + m["seam_y_after"]) / 2
        seam_gain = 1.0 - (sa / sb) if sb > 0 else 0.0
        # Flatness gain from the offset-invariant absolute-std metric; fall back to
        # CV for legacy/synthetic fixtures that predate `flat_*`. cv_gain is kept
        # only for display (it is offset-sensitive — see metrics.background_cv).
        cv_gain = 1.0 - (m["cv_after"] / m["cv_before"]) if m.get("cv_before", 0) > 0 else 0.0
        fb, fa = m.get("flat_before"), m.get("flat_after")
        if fb is not None and fa is not None and fb > 0:
            flat_gain = 1.0 - fa / fb
        else:
            flat_gain = cv_gain
        fidelity = float(m.get("fidelity", 1.0))
        artifact = 0.5 * _squash(seam_gain) + 0.5 * _squash(flat_gain)
        ranked.append({
            "name": r["name"], "seam_gain": seam_gain, "cv_gain": cv_gain,
            "flat_gain": flat_gain, "artifact": artifact, "fidelity": fidelity,
            "composite": artifact * fidelity,
            "seconds": m["seconds"], "peak_rss_mb": m["peak_rss_mb"],
        })
    ranked.sort(key=lambda d: d["composite"], reverse=True)
    return ranked


def recommendation_text(ranked):
    if not ranked:
        return "No variants produced."
    win = ranked[0]
    lines = [f"Recommended: **{html.escape(win['name'])}** — highest composite score "
             f"({win['composite']:.3f} = artifact {win['artifact']:.3f} × fidelity "
             f"{win['fidelity']:.2f}): seam suppression {win['seam_gain']*100:.1f}%, "
             f"background-flatness gain {win['flat_gain']*100:.1f}%, signal fidelity "
             f"{win['fidelity']*100:.0f}%, {win['seconds']:.1f}s, {win['peak_rss_mb']:.0f} MB peak."]
    lines.append("Composite = artifact_score × fidelity: fidelity (retained foreground "
                 "dynamic range × structure correlation, in [0,1]) MULTIPLIES the artifact "
                 "score, so a variant that suppresses seams by destroying signal cannot win — "
                 "it scores ~0. A low fidelity here means the correction ate real signal.")
    if len(ranked) > 1:
        r = ranked[1]
        faster = r["seconds"] < win["seconds"]
        lines.append(
            f"Runner-up **{html.escape(r['name'])}** ({r['composite']:.3f})"
            + (f" is faster ({r['seconds']:.1f}s vs {win['seconds']:.1f}s); "
               "prefer it if the composite gap is within visual noise and runtime matters."
               if faster else
               "; the winner leads on quality and is not slower, so prefer the winner."))
        lines.append("When composites are within ~0.01 of each other, treat them as a tie and "
                     "decide from the before/after crops and QuPath pyramids — the metrics "
                     "cannot see local artifacts the eye catches.")
    return "\n\n".join(lines)


def write_report(results, plot_index, out_html):
    ranked = rank_variants(results)
    rows = "".join(
        f"<tr><td>{i+1}</td><td>{html.escape(d['name'])}</td>"
        f"<td>{d['composite']:.3f}</td><td>{d['fidelity']*100:.0f}%</td>"
        f"<td>{d['seam_gain']*100:.1f}%</td>"
        f"<td>{d['flat_gain']*100:.1f}%</td><td>{d['seconds']:.1f}</td>"
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
<table><tr><th>#</th><th>Variant</th><th>Composite</th><th>Fidelity</th><th>Seam gain</th>
<th>Flatness gain</th><th>Seconds</th><th>Peak MB</th></tr>{rows}</table>
<h2>Diagnostics</h2>{plots_html}
</body></html>"""
    with open(out_html, "w") as f:
        f.write(doc)
    return str(out_html)
