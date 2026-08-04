#!/usr/bin/env python3
"""Compile panel.yaml -> model_config.json (+ spec_report.html). Reads NO cell data."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent / "utils"))

from phenotyping.constraints import split_constraints  # noqa: E402
from phenotyping.feasible import enumerate_feasible, validate  # noqa: E402
from phenotyping.model_config import (  # noqa: E402
    build_model_config,
    write_spec_report_html,
)
from phenotyping.palette import build_palette  # noqa: E402
from phenotyping.panel_schema import PanelError, parse_panel, typecheck  # noqa: E402
from phenotyping.references import resolve_references  # noqa: E402


def compile_panel(panel_src, *, alpha_target: float, min_calibration: int,
                  max_enumerate: int, spec_version: Optional[str] = None
                  ) -> Tuple[dict, List[str], List[str]]:
    panel = parse_panel(panel_src)
    typecheck(panel)  # §4.1 — raises PanelError on hard type errors
    feasible = enumerate_feasible(panel, max_enumerate=max_enumerate)
    split = split_constraints(panel)
    references, ref_warns = resolve_references(panel, split)
    palette = build_palette(list(panel.phenotypes))
    errors, warnings = validate(panel, feasible)
    warnings = warnings + ref_warns
    cfg = build_model_config(panel, feasible, split, references, palette,
                             alpha_target=alpha_target, min_calibration=min_calibration,
                             spec_version=spec_version or "panel@unstamped")
    return cfg, errors, warnings


def _spec_version(path: str) -> str:
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return f"panel@sha256:{digest}"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Compile panel.yaml to model_config.json")
    ap.add_argument("--panel", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--validate-only", action="store_true")
    ap.add_argument("--accept-all", action="store_true")
    ap.add_argument("--alpha-target", type=float, default=0.05)
    ap.add_argument("--min-calibration", type=int, default=50)
    ap.add_argument("--max-enumerate", type=int, default=100000)
    args = ap.parse_args(argv)

    spec_version = _spec_version(args.panel)

    try:
        cfg, errors, warnings = compile_panel(
            args.panel, alpha_target=args.alpha_target, min_calibration=args.min_calibration,
            max_enumerate=args.max_enumerate, spec_version=spec_version)
    except PanelError as e:
        # A HARD error (typecheck/enumerate_feasible raised) means there is no
        # valid model_config to write -- but the report must still render the
        # error for the user instead of a raw traceback. §4.3 intent: the
        # user sees what their panel means (or why it doesn't compile) before
        # any data is touched.
        errors = [str(e)]
        error_cfg = {"markers": {}, "feasible_set": [], "spec_version": spec_version}
        write_spec_report_html(error_cfg, errors, [], args.report)
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    cfg["compiled_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()

    with open(args.out, "w") as fh:
        json.dump(cfg, fh, indent=2)
    write_spec_report_html(cfg, errors, warnings, args.report)

    for w in warnings:
        print(f"[WARN] {w}", file=sys.stderr)
    for e in errors:
        print(f"[ERROR] {e}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
