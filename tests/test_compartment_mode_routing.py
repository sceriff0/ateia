#!/usr/bin/env python3
"""Static guard: nothing reads `params.quantify_compartments` /
`params.quantify_statistics` / `params.embed_masks` raw except the resolver
(`lib/ParamUtils.groovy`'s `compartmentMode()`) and a small, explicit allowlist
of config/module sites that legitimately keep their own read.

All THREE params are in scope, not just `quantify_compartments`: they are the
three fields `ParamUtils.compartmentMode()` resolves together into one map, and
`assemble_export.nf:78`'s original defect --
`params.embed_masks && params.quantify_compartments && Mean+Sum in params.quantify_statistics`
-- read all three raw in the SAME expression. A guard that only watched
`quantify_compartments` would pass while that exact line regressed, having
caught none of the other two names. (Confirmed directly: a planted
`params.embed_masks && params.quantify_statistics` read in
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

  2. `conf/modules.config` reading `params.quantify_statistics` directly
     in QUANTIFY's ext.args (`quantifyStatistics(params.quantify_statistics)`
     : '' }`) -- genuinely unavoidable: `conf/*.config` closures cannot see
     `lib/*.groovy` classes (a class name referenced there resolves against
     ConfigObject and fails only when the closure runs -- see CLAUDE.md's
     "Config can't see lib/ classes"), so `ext.args` MUST read params raw.
     This reason is checked, not assumed, and does NOT extend to
     `params.quantify_compartments` or `params.embed_masks`: neither is read
     anywhere in `conf/*.config` today (only a comment mentions the first
     name), so allowlisting the whole file "because config always gets a
     pass" would hide a real future regression of either. The allowlist entry
     below names the one SITE -- identified by the text that makes it
     legitimate, the `quantifyStatistics(` call -- not the file wholesale.
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
    r"(?:\w+\.)?params\.(?:quantify_compartments|quantify_statistics|embed_masks)"
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

# path-relative-to-ROOT -> {line-content marker: reason}. A SINGLE-SITE exemption,
# stricter than ALLOWED_FILES: conf/modules.config's reason (ext.args cannot
# see lib/*.groovy classes) applies to the ONE site that actually needs it,
# not the file as a whole -- conf/modules.config does not read
# params.quantify_compartments or params.embed_masks anywhere today, and a
# whole-file exemption would hide either regressing into this file silently.
#
# The site is identified by CONTENT, not by line number. It used to be pinned to an
# exact 1-based line, which is a position: nine unrelated edits ABOVE it moved that
# position (718 -> 722 -> 727 -> 786 -> 807 -> 827 -> 844 -> 856 -> 852 -> 892),
# it conflicted in all three of this branch's merges, and each forced re-pin was an
# opportunity to widen the exemption by accident. A content marker moves with the
# line it describes, so an edit above it costs nothing.
#
# Matching by content is not looser than matching by position, because the marker is
# the very thing that makes the read legitimate -- the call to conf/modules.config's
# own quantifyStatistics() helper, which exists precisely because a lib/ class is
# unreachable from an ext.args closure. A raw read added ANYWHERE else in the file
# does not carry it and is still an offender (proven by planting one), and
# test_scan_actually_finds_the_consumers pins the match count at exactly ONE, so a
# SECOND site cannot slip in behind the marker either -- which is the only thing the
# line pin gave for free.
ALLOWED_LINE_CONTENT = {
    "conf/modules.config": {
        "quantifyStatistics(": (
            "ext.args = { ... quantifyStatistics(params.quantify_statistics) ... } "
            "-- conf/*.config closures cannot see lib/*.groovy classes, so "
            "ParamUtils.statisticsList is unreachable and ext.args must read the "
            "param raw here."
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


def _is_allowed(rel_path: str, line: str) -> bool:
    if rel_path in ALLOWED_FILES:
        return True
    return any(marker in line for marker in ALLOWED_LINE_CONTENT.get(rel_path, {}))


def test_scan_actually_finds_the_consumers():
    """A scan matching nothing would pass the allowlist check vacuously."""
    sites = _read_sites()
    files = {str(path.relative_to(ROOT)) for path, _no, _line in sites}
    assert len(sites) >= 4, f"only {len(sites)} read site(s) found -- globs may be stale"
    for expected in ALLOWED_FILES:
        assert expected in files, (
            f"{expected} no longer reads any of the three compartment params "
            "raw. If it was routed through ParamUtils.compartmentMode() "
            "instead, remove its ALLOWED_FILES entry too -- an allowlist "
            "entry for a file that no longer needs it lets this test go "
            "quiet on the next regression."
        )
    for expected_file, markers in ALLOWED_LINE_CONTENT.items():
        for marker in markers:
            matched = [
                f"{path.relative_to(ROOT)}:{no}: {line.strip()}"
                for path, no, line in sites
                if str(path.relative_to(ROOT)) == expected_file and marker in line
            ]
            assert len(matched) == 1, (
                f"{expected_file} has {len(matched)} raw read(s) carrying the "
                f"exemption marker {marker!r}; exactly 1 is pinned.\n"
                "  0 means the site was routed (or the marker was renamed) and the "
                "ALLOWED_LINE_CONTENT entry is now stale -- remove it, so a real "
                "regression cannot hide behind a dead exemption.\n"
                "  2+ means a SECOND raw read appeared behind the same marker. The "
                "exact-line pin this replaced would have caught that, so it is "
                "caught here instead: route the new one through "
                "ParamUtils.compartmentMode(), or, if it is genuinely a second "
                "ext.args closure that cannot see lib/, raise the pinned count "
                "deliberately and say why.\n" + "\n".join(matched)
            )


def test_no_file_outside_the_resolver_and_allowlist_reads_it_raw():
    """The seam: every other consumer must hold ParamUtils.compartmentMode()'s
    map, not read params.quantify_compartments / params.quantify_statistics
    / params.embed_masks itself."""
    offenders = [
        f"{path.relative_to(ROOT)}:{no}: {line.strip()}"
        for path, no, line in _read_sites()
        if not _is_allowed(str(path.relative_to(ROOT)), line)
    ]
    assert not offenders, (
        f"{len(offenders)} site(s) read a compartment-quantification param raw "
        "outside lib/ParamUtils.groovy (the resolver) and the documented "
        "allowlist. Resolve it once via ParamUtils.compartmentMode(params) and "
        "thread the result down as an argument instead -- the same seam "
        "--registration_method has (subworkflows/local/registration.nf) -- "
        "rather than re-reading the raw param here.\n" + "\n".join(offenders)
    )
