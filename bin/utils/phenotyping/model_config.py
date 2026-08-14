"""Compiled model_config.json builder + human-readable spec report
(phenotyping §4).

``build_model_config`` assembles the full compiled panel artifact from the
outputs of the earlier compiler stages (panel parse, feasible-set
enumeration, constraint split, reference resolution, palette) into the
§4 JSON shape that the runtime classifier, the exporter, and the FlowPath
QuPath extension all consume verbatim. ``compiled_at`` is left ``None``
here -- the entrypoint stamps it with a real timestamp once the whole
compile succeeds, so a builder call by itself never claims a compile time.

``write_spec_report_html`` renders that dict (plus the compiler's errors and
warnings) into a static HTML page: the human's window into what the panel
means before any real data is touched. It must show every error and warning
verbatim -- no filtering or truncation -- since silently dropping one would
hide exactly the thing this report exists to surface.
"""

from __future__ import annotations

import html
import json
from typing import Dict, List

from .feasible import inherited_signature, lineage_markers
from .panel_schema import Panel


def build_model_config(
    panel: Panel,
    feasible: List[Dict],
    split: Dict,
    references: Dict[str, dict],
    palette: Dict[str, List[int]],
    *,
    alpha_target: float,
    min_calibration: int,
    spec_version: str,
) -> dict:
    """Assemble the compiled ``model_config.json`` dict (§4 shape)."""
    parents = {name: ph.parent for name, ph in panel.phenotypes.items()}
    is_leaf = {name: True for name in panel.phenotypes}
    for name in panel.phenotypes:
        parent = parents.get(name)
        if parent in is_leaf:
            is_leaf[parent] = False

    markers = {}
    for name, mk in panel.markers.items():
        markers[name] = {
            "role": mk.role,
            "compartment": mk.compartment,
            "statistic": mk.statistic,
            "neg_source": references[name]["neg_source"],
            "pos_source": references[name]["pos_source"],
        }

    phenotypes = []
    for name, ph in panel.phenotypes.items():
        phenotypes.append({
            "name": name,
            "parent": ph.parent,
            "signature": inherited_signature(panel, name),
            "color": palette.get(name, [120, 120, 120]),
            "is_leaf": is_leaf[name],
        })

    return {
        "markers": markers,
        "phenotypes": phenotypes,
        "feasible_set": feasible,
        "constraints": {
            "never": split["never"],
            "enforce": split["enforce"],
            "audit": split["audit"],
            "requires": split["requires"],
        },
        "runtime": {
            "alpha_target": alpha_target,
            "min_calibration": min_calibration,
            "statistic_default": "Median",
        },
        "palette": palette,
        "lineage_markers": lineage_markers(panel),
        "state_markers": sorted(m for m, mk in panel.markers.items() if mk.role == "state"),
        "spec_version": spec_version,
        "compiled_at": None,
    }


def write_spec_report_html(
    model_config: dict, errors: List[str], warnings: List[str], path: str
) -> None:
    """Render ``model_config`` (plus every compiler error/warning, verbatim
    and unfiltered) to a static HTML report at ``path``."""

    def _ul(items: List[str]) -> str:
        if not items:
            return "<p>none</p>"
        return "<ul>" + "".join(f"<li>{html.escape(str(i))}</li>" for i in items) + "</ul>"

    rows = "".join(
        f"<tr><td>{html.escape(n)}</td><td>{html.escape(str(m['role']))}</td>"
        f"<td>{html.escape(str(m['compartment']))}</td><td>{html.escape(str(m['statistic']))}</td>"
        f"<td>{html.escape(json.dumps(m['neg_source']))}</td>"
        f"<td>{html.escape(json.dumps(m['pos_source']))}</td></tr>"
        for n, m in model_config["markers"].items()
    )
    body = f"""<!doctype html><html><head><meta charset="utf-8"><title>Panel spec report</title></head><body>
<h1>Panel spec report</h1>
<p>spec_version: {html.escape(str(model_config.get('spec_version')))}</p>
<p>|F| = {len(model_config['feasible_set'])}</p>
<h2>Errors</h2>{_ul(errors)}
<h2>Warnings</h2>{_ul(warnings)}
<h2>Markers (resolved references)</h2>
<table border="1"><tr><th>marker</th><th>role</th><th>compartment</th><th>statistic</th><th>neg_source</th><th>pos_source</th></tr>
{rows}</table>
</body></html>"""
    with open(path, "w") as fh:
        fh.write(body)
