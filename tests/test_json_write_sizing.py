"""`f.write(json.dumps(o))` is faster than `json.dump(o, f)` -- and only safe on small outputs.

`json.dump` serialises straight into the file through many small `f.write` calls;
`json.dumps` builds the entire document as one string and writes it once. Measured on a
~49 MB document (60 000 objects, a 40-vertex ring each -- the `contours.json` /
`cells.geojson` regime):

    json.dump(o, f)           3.65 s    peak RSS  +0.3 MB
    f.write(json.dumps(o))    0.65 s    peak RSS  +156.2 MB     (5.6x faster)

So the speed-up costs roughly 3.2x the output size in peak memory. On a manifest of a few
hundred bytes that is free. On a per-cell artifact it is the wrong trade in exactly the place
this pipeline is most memory-bound -- PERF-PLAN.md measures the correct fix for EXPORT_GEOJSON
as a **streamed** write worth -1004 MB, i.e. the opposite direction.

The source review's item reads "json.dump(o,f) -> f.write(json.dumps(o)), 9 call sites in bin/,
5.3x". The speed-up reproduces (5.65x here), but "9 call sites" is the wrong unit: applying it
to the per-cell writers would trade time for a gigabyte of peak RSS.

This guard pins the boundary in both directions, so neither half can be "finished" by mistake.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Writers whose output is one entry PER CELL. These must keep streaming `json.dump`.
PER_CELL_WRITERS = {
    "bin/export_geojson.py": "cells.geojson -- one feature per cell",
    "bin/mask_to_geojson.py": "one polygon per label",
    "bin/extract_cell_properties.py": "contours.json -- one ring per cell",
}

# Writers whose output is bounded and small. These should use the fast form.
SMALL_WRITERS = {
    "bin/create_channels_manifest.py": "a channel manifest, a few hundred bytes",
    "bin/warp_seg_qc.py": "one summary record",
    "bin/utils/stage_checkpoint.py": "a stage manifest",
}

_DUMPS_WRITE = re.compile(r"\.write\(\s*json\.dumps\(")
_DUMP = re.compile(r"\bjson\.dump\(")


def test_per_cell_writers_still_stream():
    offenders = []
    for rel, why in PER_CELL_WRITERS.items():
        text = (REPO / rel).read_text()
        if _DUMPS_WRITE.search(text):
            offenders.append(f"{rel} ({why})")

    assert not offenders, (
        "these writers emit one entry per cell, so json.dumps would materialise the whole "
        "document -- measured at ~3.2x the output size in peak RSS. Stream with json.dump, or "
        "stream incrementally (PERF-PLAN measures -1004 MB for EXPORT_GEOJSON):\n  "
        + "\n  ".join(offenders)
    )


def test_small_writers_use_the_fast_form():
    offenders = []
    for rel, why in SMALL_WRITERS.items():
        text = (REPO / rel).read_text()
        if not _DUMPS_WRITE.search(text):
            offenders.append(f"{rel} ({why})")

    assert not offenders, (
        "these outputs are small and bounded, so the 5.6x faster f.write(json.dumps(...)) "
        "costs nothing:\n  " + "\n  ".join(offenders)
    )


def test_every_listed_file_exists_and_writes_json():
    """A guard naming a file that moved or stopped writing JSON checks nothing."""
    for rel in {**PER_CELL_WRITERS, **SMALL_WRITERS}:
        path = REPO / rel
        assert path.exists(), f"{rel} no longer exists -- re-point or remove this entry"
        text = path.read_text()
        assert _DUMP.search(text) or _DUMPS_WRITE.search(text), (
            f"{rel} no longer writes JSON -- remove its entry rather than leaving it to pass "
            "vacuously"
        )


def test_the_two_forms_produce_identical_bytes(tmp_path):
    """The whole premise: this is a speed/memory trade, never an output change."""
    import json

    obj = {"cells": [{"id": i, "poly": [[float(i), float(i + 1)]] * 4} for i in range(50)]}

    a, b = tmp_path / "a.json", tmp_path / "b.json"
    with open(a, "w") as f:
        json.dump(obj, f)
    with open(b, "w") as f:
        f.write(json.dumps(obj))

    assert a.read_bytes() == b.read_bytes()


def test_the_two_forms_are_identical_with_indent_too(tmp_path):
    """The small writers pass indent=2; the equivalence must hold there as well."""
    import json

    obj = {"stage": "rigid", "files": ["a", "b"], "n": 3}

    a, b = tmp_path / "a.json", tmp_path / "b.json"
    with open(a, "w") as f:
        json.dump(obj, f, indent=2)
    with open(b, "w") as f:
        f.write(json.dumps(obj, indent=2))

    assert a.read_bytes() == b.read_bytes()
