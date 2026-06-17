"""Emit a regression-derived conf/modules.optimized.config.

Reproduces the additive-sigma buffer from notebooks/resource_regression.ipynb:
    memory = { check_max( ( <input_expr>*slope + intercept + sigma*task.attempt ).GB, 'memory' ) }
Writes a SEPARATE file (never overwrites the live conf/modules.config) for review.
"""
from __future__ import annotations

from pathlib import Path

# Per-process Groovy expression for input size in GiB. Processes absent here use
# the generic `file_gb` placeholder, which the user maps to the real input var.
PROCESS_INPUT_EXPR = {
    "CONVERT_IMAGE": "(image_file.size() >> 30)",
    "PREPROCESS": "(ome_tiff.size() >> 30)",
    "SEGMENT": "(merged_file.size() >> 30)",
    "MERGE_AND_PYRAMID": "(total_gb)",
}

R2_LOW = 0.5


def memory_closure(process: str, model: dict, input_expr: str | None = None) -> str:
    expr = input_expr if input_expr is not None else PROCESS_INPUT_EXPR.get(process, "file_gb")
    slope = round(model["slope"], 3)
    intercept = round(model["intercept"], 3)
    sigma = round(model.get("sigma", 0.0), 3)
    body = (f"check_max( ( {expr} * {slope} + {intercept} + {sigma} * task.attempt ).GB, "
            f"'memory' )")
    return f"    withName: '{process}' {{\n        memory = {{ {body} }}\n    }}"


def write_optimized_config(models: dict, out_path) -> None:
    lines = [
        "// conf/modules.optimized.config",
        "// AUTO-GENERATED from benchmark regression (benchmarks/analysis). REVIEW before use.",
        "// memory = check_max((input_gb*slope + intercept + sigma*task.attempt).GB, 'memory')",
        "process {",
    ]
    for process, model in sorted(models.items()):
        r2 = model.get("r2")
        note = f"    // fit: r2={round(r2, 2) if r2 == r2 else 'n/a'}, n={model.get('n')}"
        if r2 is None or r2 != r2 or r2 < R2_LOW:
            note += "  // LOW CONFIDENCE — verify against observed peaks"
        lines.append(note)
        lines.append(memory_closure(process, model))
    lines.append("}")
    Path(out_path).write_text("\n".join(lines) + "\n")
