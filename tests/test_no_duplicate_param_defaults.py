#!/usr/bin/env python3
"""Guard against duplicated parameter defaults.

`nextflow.config`'s `params {}` block is the single source of truth for
parameter defaults. Process scripts and `conf/modules.config` must not
re-declare a *concrete* fallback via Groovy's `?:` operator, because `?:` is
falsy-coalescing (not null-coalescing): `params.tilex ?: 256` silently
rewrites a legitimate `--tilex 0` override to `256`, and if the fallback
literal ever drifts from `nextflow.config` the module lies about the real
default.

`params.x ?: <literal>` is only legitimate where `nextflow.config` declares
`x = null` — there, `?:` is the live "null means derive a value at runtime"
contract (e.g. `preproc_pool_workers`, which falls back to `task.cpus`).

This test derives its allowlist directly from `nextflow.config`'s `params {}`
block, so it keeps working whether a param is null-declared today or someone
flips it to a concrete value tomorrow (or vice versa) -- no hand-curated list
to fall out of sync.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "nextflow.config"

# `params.<name> ?:` -- the pattern under audit.
FALLBACK_RE = re.compile(r"params\.([A-Za-z_][A-Za-z0-9_]*)\s*\?:")

# Files scanned for `params.x ?: <literal>` fallbacks.
#
# `lib/*.groovy` is in the list because parameter reads move there: SegBackends holds
# the per-backend defaults SEGMENT's script block used to inline, including
# `params.instanseg_model_dir ?: "$PWD/.instanseg_cache"`. A fallback that escaped the
# scan by being one directory over would be exactly the drift this test exists to catch.
SCANNED_GLOBS = [
    "modules/local/*.nf",
    "conf/modules.config",
    "workflows/*.nf",
    "subworkflows/**/*.nf",
    "lib/*.groovy",
]


def _extract_params_block(config_text: str) -> str:
    """Return the raw text inside the top-level `params { ... }` block."""
    start = config_text.find("params {")
    if start == -1:
        raise ValueError("Could not locate `params { ... }` block in nextflow.config")

    brace_start = config_text.find("{", start)
    depth = 0
    end = None
    for i in range(brace_start, len(config_text)):
        ch = config_text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break

    if end is None:
        raise ValueError("Unclosed `params { ... }` block in nextflow.config")

    return config_text[brace_start + 1 : end]


def _strip_line_comment(line: str) -> str:
    """Strip a trailing `// ...` comment, ignoring `//` inside string literals."""
    in_single = False
    in_double = False
    i = 0
    while i < len(line) - 1:
        ch = line[i]
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "/" and line[i + 1] == "/" and not in_single and not in_double:
            return line[:i]
        i += 1
    return line


def parse_declared_params(config_text: str) -> dict[str, str]:
    """Parse the `params {}` block into {name: declared_value_text}.

    `declared_value_text` is the trimmed right-hand side of the assignment
    (comments stripped), so callers can test `value == "null"`.
    """
    block = _extract_params_block(config_text)
    declared: dict[str, str] = {}
    for raw_line in block.splitlines():
        line = _strip_line_comment(raw_line).strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$", line)
        if m:
            declared[m.group(1)] = m.group(2)
    return declared


def find_fallback_sites() -> list[tuple[Path, int, str, str]]:
    """Return (file, line_no, param_name, line_text) for every `params.x ?:` site."""
    sites: list[tuple[Path, int, str, str]] = []
    for pattern in SCANNED_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            text = path.read_text()
            for line_no, line in enumerate(text.splitlines(), start=1):
                for m in FALLBACK_RE.finditer(line):
                    sites.append((path, line_no, m.group(1), line.strip()))
    return sites


def test_no_duplicate_param_defaults():
    """Every `params.x ?: <literal>` site must name a param declared `null`.

    Where `nextflow.config` gives `x` a concrete default, any `?:` fallback
    beside `params.x` is unreachable duplication (or worse: a stale literal
    that disagrees with the real default) and must be deleted, leaving the
    bare `params.x`.
    """
    config_text = CONFIG_PATH.read_text()
    declared = parse_declared_params(config_text)
    sites = find_fallback_sites()

    assert sites, "No `params.x ?:` sites found -- scan patterns may be stale."

    offending = []
    for path, line_no, name, line_text in sites:
        if name not in declared:
            offending.append(
                f"{path.relative_to(ROOT)}:{line_no}: params.{name} ?: ... "
                f"(param not found in nextflow.config params{{}} block at all)"
            )
            continue
        if declared[name] != "null":
            offending.append(
                f"{path.relative_to(ROOT)}:{line_no}: params.{name} ?: ... "
                f"duplicates nextflow.config's concrete default "
                f"`{name} = {declared[name]}` -- delete the `?: ...` fallback."
            )

    assert not offending, (
        f"{len(offending)} site(s) duplicate a concrete nextflow.config default "
        "via `?:`. Delete the fallback literal, leaving the bare `params.x`, "
        "unless nextflow.config declares the param `null` (in which case `?:` "
        "is the live null-means-derive contract):\n" + "\n".join(offending)
    )
