#!/usr/bin/env python3
"""Static guard: nothing reads `params.quantify_compartments` /
`params.expanded_quantification` / `params.embed_masks` raw except the resolver
(`lib/ParamUtils.groovy`'s `compartmentMode()`) and a small, explicit allowlist
of config/module sites that legitimately keep their own read.

All THREE params are in scope, not just `quantify_compartments`: they are the
three fields `ParamUtils.compartmentMode()` resolves together into one map, and
`assemble_export.nf:78`'s original defect --
`params.embed_masks && params.quantify_compartments && params.expanded_quantification`
-- read all three raw in the SAME expression. A guard that only watched
`quantify_compartments` would pass while that exact line regressed, having
caught none of the other two names. (Confirmed directly: a planted
`params.embed_masks && params.expanded_quantification` read in
assemble_export.nf passed an earlier, narrower version of this guard 2/2 green.)

`quantify_compartments` used to be read directly at every consumer across
7+ files, including one ternary
(`ch_nuc_contours_for_export = params.quantify_compartments ? ... : ...`)
copied VERBATIM between postprocess.nf and add_cycle.nf, plus
assemble_export.nf's `embed_masks` gate reading all three related params raw.
Its two sibling routing params both already have a seam: --registration_method
is read ONCE (subworkflows/local/registration.nf) and handed down as an
argument; --nuclear_markers is routed through MarkerUtils.markerList() (see
test_nuclear_marker_routing.py). This test is the equivalent guard for the
compartment-quantification trio: `ParamUtils.compartmentMode(params)` is
resolved once in workflows/mirage.nf and threaded down as an argument to every
subworkflow that used to re-derive any of the three flags itself --
subworkflows/local/segmentation.nf's SEGMENTATION and
READ_SEGMENTED_CHECKPOINT, subworkflows/local/postprocess.nf,
subworkflows/local/add_cycle.nf, subworkflows/local/assemble_export.nf.

Two kinds of exception are legitimate and allowlisted below, each for a
DIFFERENT, precisely-stated reason -- conflating them is exactly the shape of
allowlist entry this repo has shipped before with a stated reason that had
counterexamples (see CLAUDE.md's verification-reality note on
`test_resource_label_coverage.py`):

  1. `modules/local/*.nf` `script:`/`stub:` blocks reading a param directly to
     build their own CLI flag string -- the established, accepted pattern in
     this repo (see CLAUDE.md: "modules/local/*.nf reading a param in a
     script: block to build its own flags is the established pattern"). Each
     such site is a leaf: it consumes the flag to render a command string and
     never re-exports the decision to another file, so no seam threading is
     possible there and none is owed.

  2. `conf/modules.config` reading `params.expanded_quantification` directly
     at line 645 (`ext.args = { params.expanded_quantification ? '--expanded'
     : '' }`) -- genuinely unavoidable: `conf/*.config` closures cannot see
     `lib/*.groovy` classes (a class name referenced there resolves against
     ConfigObject and fails only when the closure runs -- see CLAUDE.md's
     "Config can't see lib/ classes"), so `ext.args` MUST read params raw.
     This reason is checked, not assumed, and does NOT extend to
     `params.quantify_compartments` or `params.embed_masks`: neither is read
     anywhere in `conf/*.config` today (only a comment mentions the first
     name), so allowlisting the whole file "because config always gets a
     pass" would hide a real future regression of either. The allowlist entry
     below names the one line, not the file wholesale.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Every place Nextflow/Groovy code can live -- identical to
# test_nuclear_marker_routing.py's SCANNED_GLOBS. `nextflow.config` is excluded:
# it DECLARES the parameters (the only place a default may live), it does not
# consume them. `tests/` is excluded: tests may reference the params freely
# (e.g. tests/lib_probe.nf's ParamUtils.compartmentMode() assertions) without
# being a production consumer.
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

# All three fields ParamUtils.compartmentMode() resolves together -- see the
# module docstring for why watching only one of the three is not enough.
READ_RE = re.compile(
    r"(?:\w+\.)?params\.(?:quantify_compartments|expanded_quantification|embed_masks)"
)

# path-relative-to-ROOT -> reason. A WHOLE-FILE exemption: every raw read in
# this file is covered, because the reason applies uniformly to the file
# (each is its own script:/stub: leaf building its own CLI flag). A new file
# appearing here is a regression this test exists to catch, not an invitation
# to widen this dict quietly.
ALLOWED_FILES = {
    "modules/local/quantify.nf": (
        "script: block -- builds its own --nuclei_mask_file flag from "
        "params.quantify_compartments, the same way every other "
        "ext.args-shaped flag this process builds is assembled inside its "
        "own script: block."
    ),
    "modules/local/export_geojson.nf": (
        "script:/stub: blocks -- script: builds its own "
        "--nucleus_contours_json flag from params.quantify_compartments; "
        "stub: mirrors that same branch (only touches "
        "cells_wholecell.geojson when the real run would have produced it) "
        "so the -stub output tree matches the real one."
    ),
    "modules/local/export_spatialdata.nf": (
        "script: block -- builds its own --nucleus-contours-json flag from "
        "params.quantify_compartments, the same leaf pattern as "
        "export_geojson.nf."
    ),
}

# path-relative-to-ROOT -> {1-based line numbers}. An EXACT-LINE exemption,
# stricter than ALLOWED_FILES: conf/modules.config's reason (ext.args cannot
# see lib/*.groovy classes) applies to the ONE line that actually needs it,
# not the file as a whole -- conf/modules.config does not read
# params.quantify_compartments or params.embed_masks anywhere today, and a
# whole-file exemption would hide either regressing into this file silently.
ALLOWED_LINES = {
    "conf/modules.config": {
        # Line-pinned on purpose (see test_scan_actually_finds_the_consumers): an
        # exemption keyed to a file rather than a line would cover the next raw read
        # added anywhere in it. The cost is that unrelated edits above this line move
        # it -- 718 -> 722 when the reg_qc=2 QC stopped segmenting for itself and
        # SEG_QC_GEOJSON's block shrank; 722 -> 727 when feat/lsa-cell-pairing's
        # rewritten WARP_SEG_QC ext.args comment added 5 net lines above it;
        # 727 -> 736 when the three top-level config helper functions were inlined
        # for Nextflow 26's strict parser; 736 -> 744 when TILED_SOLVE gained the
        # ext.args block carrying its confidence/range gates; 744 -> 748 when that
        # same block's `?:` was replaced by an explicit null test (Elvis treats a
        # legal 0 as unset) and gained the comment explaining why; 748 -> 834 when the
        # in-process BaSiC path became the three-process nf-core BASICPY chain and
        # TILE_FOR_BASIC / BASICPY / APPLY_PROFILES gained withName blocks above it
        # (826 -> 834 once BASICPY's block gained the comment recording that running at
        # upstream defaults is a decision).
        # Re-pin, do not
        # widen. (Re-pin from the file, not by guessing:
        # `grep -n "params.expanded_quantification ?" conf/modules.config`.)
        834: (
            "ext.args = { params.expanded_quantification ? '--expanded' : "
            "'' } -- conf/*.config closures cannot see lib/*.groovy classes, "
            "so ext.args must read params raw here."
        ),
    },
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


def _is_allowed(rel_path: str, lineno: int) -> bool:
    if rel_path in ALLOWED_FILES:
        return True
    return lineno in ALLOWED_LINES.get(rel_path, set())


def test_scan_actually_finds_the_consumers():
    """A scan matching nothing would pass the allowlist check vacuously."""
    sites = _read_sites()
    files = {str(path.relative_to(ROOT)) for path, _no, _line in sites}
    lines = {(str(path.relative_to(ROOT)), no) for path, no, _line in sites}
    assert len(sites) >= 4, f"only {len(sites)} read site(s) found -- globs may be stale"
    for expected in ALLOWED_FILES:
        assert expected in files, (
            f"{expected} no longer reads any of the three compartment params "
            "raw. If it was routed through ParamUtils.compartmentMode() "
            "instead, remove its ALLOWED_FILES entry too -- an allowlist "
            "entry for a file that no longer needs it lets this test go "
            "quiet on the next regression."
        )
    for expected_file, expected_linenos in ALLOWED_LINES.items():
        for expected_lineno in expected_linenos:
            assert (expected_file, expected_lineno) in lines, (
                f"{expected_file}:{expected_lineno} no longer reads a "
                "compartment-quantification param raw. Remove its ALLOWED_LINES "
                "entry too, so a real regression on that exact line cannot hide "
                "behind a stale exemption."
            )


def test_no_file_outside_the_resolver_and_allowlist_reads_it_raw():
    """The seam: every other consumer must hold ParamUtils.compartmentMode()'s
    map, not read params.quantify_compartments / params.expanded_quantification
    / params.embed_masks itself."""
    offenders = [
        f"{path.relative_to(ROOT)}:{no}: {line.strip()}"
        for path, no, line in _read_sites()
        if not _is_allowed(str(path.relative_to(ROOT)), no)
    ]
    assert not offenders, (
        f"{len(offenders)} site(s) read a compartment-quantification param raw "
        "outside lib/ParamUtils.groovy (the resolver) and the documented "
        "allowlist. Resolve it once via ParamUtils.compartmentMode(params) and "
        "thread the result down as an argument instead -- the same seam "
        "--registration_method has (subworkflows/local/registration.nf) -- "
        "rather than re-reading the raw param here.\n" + "\n".join(offenders)
    )
