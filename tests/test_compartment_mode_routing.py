#!/usr/bin/env python3
"""Static guard: nothing reads `params.quantify_compartments` raw except the
resolver (`lib/ParamUtils.groovy`'s `compartmentMode()`) and a small, explicit
allowlist of module `script:`/`stub:` sites that legitimately keep their own
read.

`quantify_compartments` used to be read directly at every consumer across
7+ files, including one ternary
(`ch_nuc_contours_for_export = params.quantify_compartments ? ... : ...`)
copied VERBATIM between postprocess.nf and add_cycle.nf, plus
assemble_export.nf's `embed_masks` gate reading all three related params raw.
Its two sibling routing params both already have a seam: --registration_method
is read ONCE (subworkflows/local/registration.nf) and handed down as an
argument; --nuclear_markers is routed through MarkerUtils.markerList() (see
test_nuclear_marker_routing.py). This test is the equivalent guard for
quantify_compartments: `ParamUtils.compartmentMode(params)` is resolved once
in workflows/mirage.nf and threaded down as an argument to every subworkflow
that used to re-derive the flag itself --
subworkflows/local/segmentation.nf's SEGMENTATION and
READ_SEGMENTED_CHECKPOINT, subworkflows/local/postprocess.nf,
subworkflows/local/add_cycle.nf, subworkflows/local/assemble_export.nf.

Exactly one kind of exception is legitimate and allowlisted below:
`modules/local/*.nf` `script:`/`stub:` blocks reading the param directly to
build their own CLI flag string -- the established, accepted pattern in this
repo (see CLAUDE.md: "modules/local/*.nf reading a param in a script: block to
build its own flags is the established pattern"). Each such site is a leaf:
it consumes the flag to render a command string and never re-exports the
decision to another file, so no seam threading is possible there and none is
owed.

`conf/*.config` is NOT allowlisted, on purpose, even though `conf/*.config`
genuinely cannot see `lib/*.groovy` classes and so MUST read some params raw
for `ext.args` (see CLAUDE.md: "Config can't see lib/ classes"). That
exemption is real for `params.expanded_quantification`
(conf/modules.config:577), but --- checked, not assumed --- conf/modules.config
has ZERO actual `params.quantify_compartments` reads today (only a comment
mentioning the name). Naming it in ALLOWED_FILES anyway, "because config always
gets a pass", is exactly the shape of allowlist entry this repo has shipped
before with a stated reason that had counterexamples (see CLAUDE.md's
verification-reality note on `test_resource_label_coverage.py`); leaving it out
means a real future `params.quantify_compartments` read landing in
conf/modules.config still trips this test, forcing a conscious decision
instead of silently fitting an already-too-broad exemption.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Every place Nextflow/Groovy code can live -- identical to
# test_nuclear_marker_routing.py's SCANNED_GLOBS. `nextflow.config` is excluded:
# it DECLARES the parameter (the only place a default may live), it does not
# consume it. `tests/` is excluded: tests may reference the param freely (e.g.
# tests/lib_probe.nf's ParamUtils.compartmentMode() assertions) without being a
# production consumer.
SCANNED_GLOBS = [
    "main.nf",
    "modules/local/*.nf",
    "subworkflows/**/*.nf",
    "workflows/*.nf",
    "lib/*.groovy",
    "conf/*.config",
]

# The resolver itself. Its body is what "routed" means; it is not a consumer.
ROUTER_FILE = "ParamUtils.groovy"

READ_RE = re.compile(r"(?:\w+\.)?params\.quantify_compartments")

# path-relative-to-ROOT -> reason. Every remaining raw read must live in one of
# these files. A new file appearing here is a regression this test exists to
# catch, not an invitation to widen this dict quietly.
ALLOWED_FILES = {
    "modules/local/quantify.nf": (
        "script: block -- builds its own --nuclei_mask_file flag from the raw "
        "param, the same way every other ext.args-shaped flag this process "
        "builds is assembled inside its own script: block."
    ),
    "modules/local/export_geojson.nf": (
        "script:/stub: blocks -- script: builds its own --nucleus_contours_json "
        "flag; stub: mirrors that same branch (only touches "
        "cells_wholecell.geojson when the real run would have produced it) so "
        "the -stub output tree matches the real one."
    ),
    "modules/local/export_spatialdata.nf": (
        "script: block -- builds its own --nucleus-contours-json flag, the same "
        "leaf pattern as export_geojson.nf."
    ),
}


def _is_comment(line: str) -> bool:
    s = line.strip()
    return s.startswith(("//", "*", "/*", "#"))


def _read_sites() -> list[tuple[Path, int, str]]:
    """(file, 1-based line no, line text) for every raw read outside the resolver."""
    sites = []
    for pattern in SCANNED_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            if path.name == ROUTER_FILE:
                continue
            for i, line in enumerate(path.read_text().splitlines()):
                if _is_comment(line) or not READ_RE.search(line):
                    continue
                sites.append((path, i + 1, line))
    return sites


def test_scan_actually_finds_the_consumers():
    """A scan matching nothing would pass the allowlist check vacuously."""
    sites = _read_sites()
    files = {str(path.relative_to(ROOT)) for path, _no, _line in sites}
    assert len(sites) >= 3, f"only {len(sites)} read site(s) found -- globs may be stale"
    for expected in ALLOWED_FILES:
        assert expected in files, (
            f"{expected} no longer reads params.quantify_compartments raw. If it "
            "was routed through ParamUtils.compartmentMode() instead, remove its "
            "ALLOWED_FILES entry too -- an allowlist entry for a file that no "
            "longer needs it lets this test go quiet on the next regression."
        )


def test_no_file_outside_the_resolver_and_allowlist_reads_it_raw():
    """The seam: every other consumer must hold ParamUtils.compartmentMode()'s
    map, not read params.quantify_compartments itself."""
    offenders = [
        f"{path.relative_to(ROOT)}:{no}: {line.strip()}"
        for path, no, line in _read_sites()
        if str(path.relative_to(ROOT)) not in ALLOWED_FILES
    ]
    assert not offenders, (
        f"{len(offenders)} site(s) read params.quantify_compartments raw outside "
        "lib/ParamUtils.groovy (the resolver) and the documented allowlist. "
        "Resolve it once via ParamUtils.compartmentMode(params) and thread the "
        "result down as an argument instead -- the same seam --registration_method "
        "has (subworkflows/local/registration.nf) -- rather than re-reading the "
        "raw param here.\n" + "\n".join(offenders)
    )
