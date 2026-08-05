#!/usr/bin/env python3
"""Guard against process labels that resolve to no `withLabel` selector.

A Nextflow `label` that matches no `withLabel:` selector is not an error --
the process silently falls through to `conf/base.config`'s defaults. That
makes an unmatched label invisible at runtime: it never crashes, it just
quietly under- (or over-) provisions the process.

This bit `modules/local/export_spatialdata.nf`, whose `label` line is a
ternary choosing between `'process_high_memory'` and `'process_medium'`.
`process_high_memory` was never defined in `conf/modules.config` -- it only
ever existed in CLAUDE.md's prose -- so the branch meant to request *more*
memory silently fell back to base.config's 6 GB default, 1/33rd of the
cheaper branch's 200 GB.

This test asserts every resource label referenced in `modules/local/*.nf`
resolves to a `withLabel` selector defined in `conf/modules.config`. Because
a `label` line can be an expression (a ternary, as above) rather than a bare
literal, extraction pulls *every* quoted string off a `label` line, not just
a single literal -- otherwise the guard would miss exactly the shape that
caused this bug.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULES_DIR = ROOT / "modules" / "local"
MODULES_CONFIG = ROOT / "conf" / "modules.config"

# A `label ...` statement, however its right-hand side is built (bare
# literal, ternary, etc.) -- anchored to line start so we don't match
# `label` appearing elsewhere (e.g. inside a string).
LABEL_LINE_RE = re.compile(r"^\s*label\s+.+$")

# Every single- or double-quoted string literal on a line.
QUOTED_STRING_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"")

# `withLabel: 'name' { ... }` selectors in conf/modules.config.
WITH_LABEL_RE = re.compile(r"withLabel:\s*(?:'([^']+)'|\"([^\"]+)\")")


def extract_labels_from_line(line: str) -> list[str]:
    """Return every quoted string literal on a `label ...` line.

    A bare `label 'process_medium'` yields one label. A ternary like
    `label cond ? 'process_high_memory' : 'process_medium'` yields both
    branches -- this is the shape that produced the original bug, so any
    extraction that only handles a single literal would miss it.
    """
    return [m.group(1) if m.group(1) is not None else m.group(2) for m in QUOTED_STRING_RE.finditer(line)]


def find_module_label_sites() -> list[tuple[Path, int, str, list[str]]]:
    """Return (file, line_no, line_text, labels) for every `label` line in modules/local/*.nf."""
    sites: list[tuple[Path, int, str, list[str]]] = []
    for path in sorted(MODULES_DIR.glob("*.nf")):
        for line_no, line in enumerate(path.read_text().splitlines(), start=1):
            if LABEL_LINE_RE.match(line):
                sites.append((path, line_no, line.strip(), extract_labels_from_line(line)))
    return sites


def find_defined_labels() -> set[str]:
    """Return the set of labels with a `withLabel:` selector in conf/modules.config."""
    text = MODULES_CONFIG.read_text()
    return {m.group(1) if m.group(1) is not None else m.group(2) for m in WITH_LABEL_RE.finditer(text)}


def test_extract_labels_handles_ternary_expression():
    """Extraction must find both branches of a ternary `label` line, not just one literal.

    This is a regression guard on the extractor itself: a naive
    `label\\s+'([^']+)'`-style pattern (single literal only) would silently
    return just one match -- or none -- for the exact line shape that
    caused the export_spatialdata.nf defect, defeating the guard below.
    """
    ternary_line = "    label params.spatialdata_include_image ? 'process_high_memory' : 'process_medium'"
    assert extract_labels_from_line(ternary_line) == ["process_high_memory", "process_medium"]

    literal_line = "    label 'process_high'"
    assert extract_labels_from_line(literal_line) == ["process_high"]


def test_every_module_label_resolves_to_a_conf_modules_config_selector():
    """Every label referenced in modules/local/*.nf must resolve in conf/modules.config.

    An unmatched label is not a Nextflow error -- the process silently
    inherits conf/base.config's defaults instead of the resources its label
    name implies. This is exactly how export_spatialdata.nf's
    'process_high_memory' branch OOM-prone-ly ran on a 6 GB default instead
    of a high-memory tier.
    """
    defined = find_defined_labels()
    sites = find_module_label_sites()

    assert sites, "No `label` lines found under modules/local/*.nf -- scan pattern may be stale."
    assert defined, "No `withLabel:` selectors found in conf/modules.config -- scan pattern may be stale."

    offending = []
    for path, line_no, line_text, labels in sites:
        for label in labels:
            if label not in defined:
                offending.append(
                    f"{path.relative_to(ROOT)}:{line_no}: label '{label}' has no "
                    f"`withLabel: '{label}'` selector in conf/modules.config "
                    f"(line: {line_text})"
                )

    assert not offending, (
        f"{len(offending)} label reference(s) resolve to no `withLabel` selector "
        "in conf/modules.config, so the process silently falls back to "
        "conf/base.config's defaults instead of the resources the label name "
        "implies. Define the selector or re-point the module at an existing "
        "label:\n" + "\n".join(offending)
    )
