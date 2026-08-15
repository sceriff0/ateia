"""The channel SET a slide declares survives preprocessing unchanged.

WHY THIS FILE EXISTS. `PanelSignature.requireUniqueWithinPatient` refuses a
patient whose slides do not declare distinct panels, and it is called on
POST-preprocessing metas (`subworkflows/local/register_patient.nf`, and again at
`subworkflows/local/adapters/valis_adapter.nf`'s demux). A review of that check
raised a run-aborting concern: if preprocessing REBOUND `meta.channels` to a
SMALLER list -- dropping the nuclear/fiducial channel -- then two slides of one
patient whose panels differ ONLY in that channel (`DAPI|CD3|CD8` and
`CELLTOX|CD3|CD8`) would collapse to one signature at the call site, and a run
that used to complete would now abort at registration.

The concern is NOT real, and this file pins the two facts that make it
unreachable, so nobody has to re-derive them across four files again:

  1. CONVERT_IMAGE PERMUTES, IT DOES NOT FILTER. `bin/convert_image.py` moves the
     nuclear channel to index 0 -- an invariant segmentation and the tiled
     fiducial rely on -- and writes THAT list to `<prefix>_channels.txt`. The move
     is a pop-then-insert on a copy, so the output is a permutation of the input:
     same set, different order. `utils.metadata.nuclear_first` is that step,
     extracted so the invariant can be asserted rather than eyeballed.

  2. NOTHING ELSE REBINDS `meta.channels` ON THE REGISTRATION PATH.
     `subworkflows/local/preprocess.nf` rebinds it once, to exactly the list read
     back from that channels file; `modules/local/preprocess.nf` (BaSiC) passes
     `val(meta)` through untouched, and is skipped entirely under
     `--skip_preprocessing`. So the meta reaching either call site carries a
     permutation of the declared channels and nothing else.

`PanelSignature.ofChannels` lowercases and `toSorted()`s before joining, so a
permutation is the SAME signature. Declared identity therefore survives to the
check verbatim, and the two panels above stay distinct. The Groovy half of this
-- that a post-rebind pair really is accepted -- is asserted in
`tests/lib_probe.nf`, which pytest cannot reach (no lib/ on its classpath).

WHERE THE NUCLEAR CHANNEL IS ACTUALLY DROPPED: `SPLIT_CHANNELS`, via
`ChannelName.outputStems` (`modules/local/split_channels.nf`), for non-reference
slides only -- which is in POSTPROCESSING, downstream of every PanelSignature
call site. `register_patient.nf` used to attribute that drop to preprocessing.
"""

from __future__ import annotations

import re
from pathlib import Path

from utils.metadata import nuclear_first, pick_nuclear_index

ROOT = Path(__file__).resolve().parent.parent


# (declared channels, nuclear markers) pairs covering the shapes convert_image.py
# actually resolves: nuclear last, nuclear already first, the second-preference
# marker, a single-channel slide, and a repeated marker name (the duplicate branch
# in convert_to_ome_tiff's reorder).
CASES = [
    (["CD3", "CD8", "DAPI"], ["DAPI", "CELLTOX"]),
    (["DAPI", "CD3", "CD8"], ["DAPI", "CELLTOX"]),
    (["CD3", "CELLTOX", "CD8"], ["DAPI", "CELLTOX"]),
    (["DAPI"], ["DAPI", "CELLTOX"]),
    (["CD3", "CD3", "DAPI"], ["DAPI", "CELLTOX"]),
]


def _signature(channels) -> str:
    """PanelSignature.ofChannels, in Python. Same rule: trim, lowercase, sort, join."""
    return "_".join(sorted(str(c).strip().lower() for c in channels))


def test_conversion_permutes_the_channel_list_never_shrinks_it():
    """Fact 1: the list CONVERT_IMAGE writes is a permutation of the declared one."""
    for declared, markers in CASES:
        out = nuclear_first(declared, pick_nuclear_index(declared, markers))
        assert sorted(out) == sorted(declared), (
            f"conversion changed the channel SET: {declared} -> {out}. "
            "PanelSignature is computed on post-conversion metas, so anything that "
            "drops a channel here silently merges two slides' identities."
        )
        assert len(out) == len(declared)


def test_conversion_puts_the_nuclear_channel_first():
    """The reason the permutation exists at all -- channel 0 = nuclear marker."""
    for declared, markers in CASES:
        idx = pick_nuclear_index(declared, markers)
        out = nuclear_first(declared, idx)
        assert out[0] == declared[idx], f"{declared} -> {out}: nuclear not at index 0"


def test_panel_signature_is_unchanged_by_conversion():
    """Fact 1 restated in the terms the check uses: a permutation is one signature."""
    for declared, markers in CASES:
        out = nuclear_first(declared, pick_nuclear_index(declared, markers))
        assert _signature(out) == _signature(declared)


def test_two_slides_differing_only_in_nuclear_marker_stay_distinct():
    """The reviewer's exact pair. It must NOT collapse, at the call site's metas."""
    a = ["DAPI", "CD3", "CD8"]
    b = ["CELLTOX", "CD3", "CD8"]
    markers = ["DAPI", "CELLTOX"]
    sig_a = _signature(nuclear_first(a, pick_nuclear_index(a, markers)))
    sig_b = _signature(nuclear_first(b, pick_nuclear_index(b, markers)))
    assert sig_a != sig_b, (
        "two slides differing only in their nuclear marker collapsed to one "
        "signature after conversion -- PanelSignature.requireUniqueWithinPatient "
        "would abort a legitimate patient at registration."
    )


def test_convert_image_builds_its_output_channels_through_nuclear_first():
    """The function asserted above must be the one actually on the pipeline path.

    Without this, `nuclear_first` could be a private copy that agrees with nothing,
    and every assertion above would be about dead code.
    """
    src = (ROOT / "bin" / "convert_image.py").read_text()
    assert re.search(r"output_channels\s*=\s*nuclear_first\(", src), (
        "bin/convert_image.py builds output_channels some other way now -- "
        "re-derive whether it still preserves the channel set before deleting this."
    )
    assert re.search(r"from metadata import[^\n]*nuclear_first", src), (
        "bin/convert_image.py must import nuclear_first from utils.metadata rather "
        "than define its own copy: a private copy is free to disagree with the one "
        "these tests assert."
    )


# Every production rebind of a meta's `channels` key, as `path -> matched text`.
# Fact 2: this is the whole list. A THIRD entry appearing is the regression this
# test exists to catch -- it would be a new opportunity for the channel list a
# PanelSignature call site sees to stop being the declared set.
KNOWN_CHANNEL_REBINDS = {
    "subworkflows/local/preprocess.nf": (
        "+ [channels:",
        "the ONE rebind on the registration path: output_channels is read straight "
        "back from CONVERT_IMAGE's <prefix>_channels.txt, i.e. the permutation "
        "asserted above.",
    ),
    "subworkflows/local/add_cycle.nf": (
        "channels: []",
        "the FROZEN prior reference pyramid read out of --prior_outdir. It is not a "
        "declared slide and carries no panel; there is exactly one per patient "
        "group, so its empty signature cannot collide with a second one.",
    ),
}

SCANNED_GLOBS = [
    "main.nf",
    "modules/local/*.nf",
    "subworkflows/**/*.nf",
    "workflows/*.nf",
]

# The two ways Groovy code can write a meta's channel list. The first is the
# mutation idiom CLAUDE.md mandates ("Mutate with `meta + [k: v]`, never
# `meta.clone()`"), matched across newlines so a rebind split over several lines is
# still seen; the second is direct assignment, which the idiom forbids but the
# language allows. `\bchannels` cannot match inside `pyramid_channels`, which is a
# different key entirely.
REBIND_PATTERNS = [
    re.compile(r"\+\s*\[[^\]]{0,400}?\bchannels\s*:", re.DOTALL),
    re.compile(r"\.channels\s*=(?!=)"),
]


def _blank_out(text: str, pattern: re.Pattern) -> str:
    """Replace every match with its own newlines only, so line numbers survive."""
    return pattern.sub(lambda m: "\n" * m.group(0).count("\n"), text)


def _strip_noise(text: str) -> str:
    """Comments and multi-line strings removed, line numbering preserved.

    Necessary, not decorative: without it this scan reported three sites that are
    prose -- a `channels:` inside VALIS_ADAPTER's heredoc error message, a
    `channels: Map` inside a trailing `//` comment in final_qc.nf, and a
    `- channels:` bullet in preprocess.nf's block header. A guard whose only
    findings are comments is a guard the next person switches off.
    """
    for pattern in (
        re.compile(r"/\*.*?\*/", re.DOTALL),
        re.compile(r'""".*?"""', re.DOTALL),
        re.compile(r"'''.*?'''", re.DOTALL),
        re.compile(r"//[^\n]*"),
    ):
        text = _blank_out(text, pattern)
    return text


def _rebind_sites() -> list[tuple[str, int, str]]:
    """(rel path, 1-based line no, source line) for every write of a channel list."""
    sites = []
    for pattern in SCANNED_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            raw = path.read_text()
            text = _strip_noise(raw)
            for rebind_re in REBIND_PATTERNS:
                for match in rebind_re.finditer(text):
                    lineno = text.count("\n", 0, match.start()) + 1
                    line = raw.splitlines()[lineno - 1].strip()
                    sites.append((str(path.relative_to(ROOT)), lineno, line))
    return sites


def test_the_rebind_scan_finds_the_known_sites():
    """A scan matching nothing would pass the test below vacuously."""
    found = {path for path, _no, _line in _rebind_sites()}
    for expected in KNOWN_CHANNEL_REBINDS:
        assert expected in found, (
            f"{expected} no longer rebinds a meta's channels. If the rebind moved, "
            "this guard is now pointing at nothing -- re-derive where the channel "
            "list a PanelSignature call site sees comes from, and re-pin it here."
        )


def test_no_unknown_code_rebinds_meta_channels():
    """Fact 2: only the two documented sites may rewrite a slide's channel list.

    A new one is not necessarily wrong, but it is exactly where the reviewer's
    concern would become real -- so it must be looked at, and its effect on the
    signature stated here, rather than landing silently.
    """
    offenders = [
        f"{path}:{no}: {line}"
        for path, no, line in _rebind_sites()
        if not (
            path in KNOWN_CHANNEL_REBINDS and KNOWN_CHANNEL_REBINDS[path][0] in line
        )
    ]
    assert not offenders, (
        f"{len(offenders)} unrecognised rebind(s) of a meta's `channels` key.\n"
        "PanelSignature.requireUniqueWithinPatient runs on POST-preprocessing metas, "
        "so whatever this writes IS a slide's identity at registration. If it "
        "preserves the declared channel SET, add it to KNOWN_CHANNEL_REBINDS with "
        "that reason; if it does not, two slides that differ only in the removed "
        "channel now abort the run.\n" + "\n".join(offenders)
    )
