#!/usr/bin/env python3
"""Static guard: nothing reads `params.nuclear_markers` raw.

`params.nuclear_markers` arrives in three shapes and two of them break SILENTLY, so
`lib/MarkerUtils.groovy`'s `markerList()` is the only sanctioned way to read it:

  ['DAPI','CELLTOX']   nextflow.config / a params file -- the intended shape
  'CELLTOX'            `--nuclear_markers CELLTOX` (nextflow_schema.json permits a
                       string, because an array cannot be given on a command line)
  ['DAPI,CELLTOX']     a params file with the list written as one comma-joined string

This test exists because of a real defect, not a hypothetical one.
`modules/local/convert_image.nf` read the parameter raw and called `.join(' ')` on it.
For the List shape that is correct and every test passed. For the String shape it does
NOT throw: Groovy dispatches `"CELLTOX".join(' ')` to Java's static
`String.join(CharSequence, CharSequence...)` with zero varargs, which returns the EMPTY
string -- so the rendered command carried a bare `--nuclear-markers` and
`bin/convert_image.py`'s `nargs="+"` exited 2, mid-run, with an error naming the wrong
problem. Every test for the string spelling was `-stub`, and `-stub` never evaluates a
`script:` block, so nothing caught it. A static scan does not care which block the code
lives in, which is exactly why this guard is static.

Two rules, both mechanical:

  A. A raw read may never be followed directly by `.` or `[`. That is the defect shape:
     any method call or index on the un-normalised parameter.
  B. Every read must be routed -- passed to `MarkerUtils.`/`CsvUtils.` (whose entry
     points all funnel into `markerList`), on the same line or as an argument line of a
     multi-line call to one of them.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Every place Nextflow/Groovy code can live. `nextflow.config` is excluded: it DECLARES
# the parameter (that is its job and the only place a default may live) rather than
# consuming it.
SCANNED_GLOBS = [
    "main.nf",
    "modules/local/*.nf",
    "subworkflows/**/*.nf",
    "workflows/*.nf",
    "lib/*.groovy",
    "conf/*.config",
]

# The router itself. Its mentions of the parameter are documentation and the text of the
# "no markers configured" exception -- it never consumes the value, it defines what
# consuming it means. Excluded by name rather than by a fragile in-a-string-literal
# heuristic; `test_scan_actually_finds_the_consumers` keeps the exclusion from widening.
ROUTER_FILE = "MarkerUtils.groovy"

READ_RE = re.compile(r"(?:\w+\.)?params\.nuclear_markers")
# Rule A: the raw read with a method call or an index hung directly off it.
RAW_USE_RE = re.compile(r"(?:\w+\.)?params\.nuclear_markers\s*(?:\.\w|\[)")
# `Checkpoint.read` is a router too, not an exception to the rule: it takes
# nuclear_markers only to hand it to CsvUtils.countChannelsPerPatient and
# CsvUtils.parseMetadata, both of which funnel into markerList. An allowlist entry
# whose stated reason nothing checks is how these guards go quiet, so
# `test_checkpoint_read_really_routes_to_csvutils` below asserts that funnel exists
# rather than taking this comment's word for it.
# `ChannelName.outputStems` is a router for the same reason: it takes nuclear_markers
# only to ask `MarkerUtils.isNuclear` which channels a slide keeps, then maps that
# answer through its own sanitiser. It is SPLIT_CHANNELS' stub's one read of the
# parameter. `test_channelname_outputstems_really_routes_to_markerutils` pins the
# delegation instead of trusting this comment.
ROUTERS = (
    "MarkerUtils.",
    "CsvUtils.",
    "Checkpoint.read",
    "ChannelName.outputStems",
)
# Rule B fallback: a bare argument line inside a multi-line call.
BARE_ARG_RE = re.compile(r"^\(?\s*(?:\w+\.)?params\.nuclear_markers\s*[,)]?\s*$")
# How far back a bare argument line may look for its callee.
CALL_LOOKBACK = 10


def _is_comment(line: str) -> bool:
    s = line.strip()
    return s.startswith(("//", "*", "/*", "#"))


def _read_sites() -> list[tuple[Path, int, str, list[str]]]:
    """(file, 1-based line no, line text, preceding lines) for every read."""
    sites = []
    for pattern in SCANNED_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            if path.name == ROUTER_FILE:
                continue
            lines = path.read_text().splitlines()
            for i, line in enumerate(lines):
                if _is_comment(line) or not READ_RE.search(line):
                    continue
                start = max(0, i - CALL_LOOKBACK)
                sites.append((path, i + 1, line, lines[start:i]))
    return sites


def test_scan_actually_finds_the_consumers():
    """A scan that matches nothing would pass both rules vacuously."""
    sites = _read_sites()
    files = {path.name for path, _no, _line, _prev in sites}
    assert len(sites) >= 6, f"only {len(sites)} read sites found -- globs may be stale"
    # The consumers this task routed. If one disappears, that is a real change worth
    # noticing rather than a scan silently going quiet.
    # seg_qc_geojson.nf was on this list until the reg_qc=2 QC stopped segmenting for
    # itself. It read params.nuclear_markers only to tell its own StarDist invocation
    # which channel was nuclear; that invocation is now SEGMENT's (aliased
    # SEG_QC_SEGMENT), so the read moved to the place that already owned it --
    # SegBackends.groovy, still on this list, where the stardist guard and the cellsam
    # index both route through MarkerUtils. Removed rather than replaced: nothing new
    # reads the parameter, one duplicate reader stopped.
    for expected in (
        "convert_image.nf",
        "split_channels.nf",
        "SegBackends.groovy",
        "input_check.nf",
        # The two checkpoint readers, which pass the parameter to Checkpoint.read.
        "segmentation.nf",
        "add_cycle.nf",
    ):
        assert expected in files, f"{expected} no longer reads params.nuclear_markers"


def test_no_method_call_on_the_raw_parameter():
    """Rule A -- the shape of the convert_image.nf defect."""
    offenders = [
        f"{path.relative_to(ROOT)}:{no}: {line.strip()}"
        for path, no, line, _prev in _read_sites()
        if RAW_USE_RE.search(line)
    ]
    assert not offenders, (
        f"{len(offenders)} site(s) call a method on (or index) params.nuclear_markers "
        "directly. The parameter may be a List, a bare String, or a one-element "
        "comma-joined String -- `.join(' ')` on the String shape silently returns the "
        "EMPTY string. Wrap it: MarkerUtils.markerList(params.nuclear_markers).\n"
        + "\n".join(offenders)
    )


def test_every_read_is_routed_through_markerutils():
    """Rule B -- no sixth consumer can appear without going through the one rule."""
    offenders = []
    for path, no, line, prev in _read_sites():
        if any(r in line for r in ROUTERS):
            continue
        if BARE_ARG_RE.match(line.strip()) and any(
            r in p for p in prev for r in ROUTERS
        ):
            continue  # argument line of a multi-line CsvUtils/MarkerUtils call
        offenders.append(f"{path.relative_to(ROOT)}:{no}: {line.strip()}")

    assert not offenders, (
        f"{len(offenders)} site(s) read params.nuclear_markers without routing it "
        "through MarkerUtils (directly, or via a CsvUtils entry point that does). "
        "markerList() is the single place the parameter's three possible shapes are "
        "normalised; a site that bypasses it is a silent divergence, not an error.\n"
        + "\n".join(offenders)
    )


# The two CsvUtils entry points Checkpoint.read hands nuclear_markers to. Both funnel
# into MarkerUtils.markerList: countChannelsPerPatient via splitOutputChannels,
# parseMetadata via validateMetadata's hasNuclear check.
CHECKPOINT_READ_FUNNELS = (
    "CsvUtils.countChannelsPerPatient(path, imageCol, nuclear, NO_AUTO_REFERENCE)",
    "CsvUtils.parseMetadata(row, nuclear, ctx)",
)


def test_checkpoint_read_really_routes_to_csvutils():
    """The reason `Checkpoint.read` is in ROUTERS, checked rather than asserted.

    Two call sites pass `params.nuclear_markers` straight into `Checkpoint.read`, and
    Rule B accepts those lines only because that method funnels the value onward. If
    read() ever stopped doing so -- inlining its own channel-count arithmetic, say --
    Rule B would keep passing on the strength of a name, which is precisely the
    "allowlist entry whose stated reason no longer holds" failure this file's other
    tests exist to prevent. Pin the funnel itself.

    Watched fail: changing read()'s countChannelsPerPatient call to pass a literal
    marker list instead of `nuclear` fails this test on that exact string, while all
    three tests above stay green.
    """
    src = (ROOT / "lib" / "Checkpoint.groovy").read_text()
    read_at = src.index("static List<List> read(")
    body = src[read_at:]

    missing = [call for call in CHECKPOINT_READ_FUNNELS if call not in body]
    assert not missing, (
        "Checkpoint.read is in ROUTERS because it hands nuclear_markers to CsvUtils "
        "entry points that funnel into MarkerUtils.markerList. It no longer makes "
        f"{len(missing)} of those call(s) with the value bound from opts:\n"
        + "\n".join(missing)
        + "\nEither restore the routing or take Checkpoint.read out of ROUTERS."
    )


# The one MarkerUtils call ChannelName.outputStems must keep making. It exists so the
# stub of SPLIT_CHANNELS drops exactly the channels the real split drops -- the same
# rule CsvUtils.countChannelsPerPatient sizes the patient's groupTuple from.
CHANNELNAME_FUNNEL = "MarkerUtils.isNuclear(ch, nuclearMarkers)"


def test_channelname_outputstems_really_routes_to_markerutils():
    """The reason `ChannelName.outputStems` is in ROUTERS, checked not asserted.

    outputStems answers "which channels does this slide write, as filenames". The
    filename half is ChannelName's own; the WHICH half must stay MarkerUtils'. If it
    grew its own `'DAPI' in name` test, Rule B would keep passing on the strength of a
    name while the stub and the real split disagreed about which channels exist -- and
    an under-count on that grouping aborts the run (see quantify_markers.nf).

    Watched fail: replacing the isNuclear call with a literal DAPI test fails here on
    that exact string while every other test in this file stays green.
    """
    src = (ROOT / "lib" / "ChannelName.groovy").read_text()
    at = src.index("static List<String> outputStems(")
    body = src[at:]
    assert CHANNELNAME_FUNNEL in body, (
        "ChannelName.outputStems is in ROUTERS because it hands nuclear_markers to "
        f"MarkerUtils.isNuclear. It no longer makes that call ({CHANNELNAME_FUNNEL}). "
        "Either restore the routing or take ChannelName.outputStems out of ROUTERS."
    )
