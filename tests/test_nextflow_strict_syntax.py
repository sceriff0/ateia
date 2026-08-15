#!/usr/bin/env python3
"""Guards for Nextflow 26's strict (v2) parser.

Nextflow 26 makes the v2 syntax parser the only parser. It is far stricter than
the Groovy-derived v1 one, and several constructs this repo relied on are simply
rejected — config parsing aborts before a single process is instantiated, so a
violation is a hard `ERROR ~ Config parsing failed`, not a warning.

Reproduce the current parser's verdict on this tree at any time with:

    NXF_SYNTAX_PARSER=v2 nextflow -q run . -profile test -stub

The rules below are the ones that bit. Each is its own test function so a
failure names the rule that broke rather than "strict syntax is unhappy";
later rules for the `.nf` sources are appended to this same file.

Plain pytest, no pipeline runtime and no Nextflow install required — all paths
are derived from `__file__`, per this repo's other guard tests (see
test_layout.py, test_ci_stack_pinned.py).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONF_DIR = ROOT / "conf"

# A function declaration at the top level of a config file. Under the strict
# parser this is `Unexpected input: '('`, reported against the open paren.
#
# Matched with a leading `\s*` rather than anchored hard at column 0: an
# indented declaration is just as illegal, and matching only column 0 would
# make the guard silently miss one.
_CONFIG_DEF_FUNCTION_RE = re.compile(r"^\s*def\s+\w+\s*\(")


def _config_files() -> list[Path]:
    """Every parsed config file. `conf/site.config.template` is deliberately not
    matched by the glob — it is a template to copy, never included."""
    return sorted(CONF_DIR.glob("*.config"))


def test_config_files_are_present_to_scan():
    """A sweep over an empty file list passes vacuously. This is the guard on
    the guard: if the glob ever stops finding conf/modules.config, the rule
    below is not checking anything and this fails instead."""
    files = _config_files()
    assert files, f"no conf/*.config files found under {CONF_DIR}"
    assert CONF_DIR / "modules.config" in files, (
        "conf/modules.config is not in the scanned set: " f"{[f.name for f in files]}"
    )


def test_rule_1_no_top_level_def_functions_in_config():
    """Rule 1 — no `def helper(...)` in any `conf/*.config`.

    There is no legal way to share a helper between config blocks under the
    strict parser, so the fix is always to inline the body at each call site:

      * `def helper(params) { ... }`  -> `Unexpected input: '('`
      * `helper = { ... }`            -> `Dynamic config options only allowed
                                          in process scope`
      * moving it to `lib/*.groovy`   -> not visible from conf/*.config at all;
                                         the name resolves silently against the
                                         enclosing ConfigObject and blows up
                                         only when the closure runs, with a
                                         misleading MissingMethodException.
    """
    offenders: list[str] = []
    for path in _config_files():
        lines = path.read_text().splitlines()
        for lineno, line in enumerate(lines, start=1):
            if _CONFIG_DEF_FUNCTION_RE.match(line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")

    assert not offenders, (
        "Nextflow 26's strict config parser rejects a function declaration in a "
        "config file with `Unexpected input: '('`. Inline the body at each call "
        "site instead — no other form is legal (see this test's docstring). "
        "Offenders:\n  " + "\n  ".join(offenders)
    )
