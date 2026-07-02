"""Emit a regression-derived conf/modules.optimized.config.

Reproduces the additive-sigma buffer from notebooks/resource_regression.ipynb:
    memory = { check_max( ( <input_expr>*slope + intercept + sigma*task.attempt ).GB, 'memory' ) }
Writes a SEPARATE file (never overwrites the live conf/modules.config) for review.
"""
from __future__ import annotations

from pathlib import Path

# Per-process Groovy expression for input size in GiB, using the process's real
# input variable(s). Processes NOT listed here are emitted as inert COMMENTED
# blocks (the user supplies the expression and uncomments) so the generated
# config is always valid Groovy as-is.
#
# The model is fit on CONTINUOUS GiB (bytes / 2**30 in load.parse_size_logs), so
# the deployed expression must also be continuous: `size() / (1024 ** 3)` (Groovy
# `/` on longs yields a fractional BigDecimal). The earlier `(size() >> 30) ?: 1`
# floored to whole GiB and forced a 1 GiB minimum, so slope*x + intercept diverged
# from the fitted line for sub-GiB / fractional inputs. No `?: 1` guard is needed:
# a 0-byte input yields input term 0 and memory falls back to intercept + buffer.
PROCESS_INPUT_EXPR = {
    "CONVERT_IMAGE": "(image_file.size() / (1024 ** 3))",
    "PREPROCESS": "(ome_tiff.size() / (1024 ** 3))",
    "SEGMENT": "(merged_file.size() / (1024 ** 3))",
}

R2_LOW = 0.5


def memory_closure(process: str, model: dict, input_expr: str | None = None) -> str:
    slope = round(model["slope"], 3)
    intercept = round(model["intercept"], 3)
    sigma = round(model.get("sigma", 0.0), 3)
    expr = input_expr if input_expr is not None else PROCESS_INPUT_EXPR.get(process)
    if expr is None:
        # No known input-size expression: emit an INERT commented block so the
        # config stays valid Groovy. The operator fills in the real expression.
        body = (f"check_max( ( <input_gb> * {slope} + {intercept} + "
                f"{sigma} * task.attempt ).GB, 'memory' )")
        return ("    // TODO set the input-size (GiB) expression for this process, then uncomment:\n"
                f"    // withName: '{process}' {{\n"
                f"    //     memory = {{ {body} }}\n"
                "    // }")
    body = (f"check_max( ( {expr} * {slope} + {intercept} + "
            f"{sigma} * task.attempt ).GB, 'memory' )")
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
