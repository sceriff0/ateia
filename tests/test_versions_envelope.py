#!/usr/bin/env python3
"""Guard: no module hand-writes the versions.yml envelope.

Every process in modules/local/ used to write the heredoc twice — once in script:, once
in stub: with values replaced by `stub`. Because `-stub` never evaluates a script: block,
a tool present in one and absent from the other was invisible to the entire blocking CI
gate. lib/ProcessEnvelope.groovy renders both from one list, so this file's job is to keep
anyone from re-introducing a hand-written copy.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULES = sorted((ROOT / "modules" / "local").glob("*.nf"))

# Documented exceptions -- not oversights:
#
# segment.nf renders its version ROWS from lib/SegBackends.groovy's `versions` map, and
# those entries are pre-rendered YAML lines (e.g. a full
# 'deepcell: $(python -c "import deepcell; ...")' string), not plain python module
# names. ProcessEnvelope.versions()/versionsStub() only know how to turn a bare module
# name into a probe; routing segment.nf through them would mean teaching SegBackends to
# speak ProcessEnvelope's vocabulary instead, which is a rewrite of a class that predates
# this task and belongs to Task 3's neighbour, not this one.
#
# aggregate_size_logs.nf runs in `container 'ubuntu:22.04'` and reports a `bash:` version
# row, not a `python:` one -- it has no Python interpreter at all. ProcessEnvelope always
# prepends a `python:` row (25 of 28 modules had one; this is the genuine anomaly), so
# routing this module through it would add a fabricated/`unknown` python entry to a
# published report that has never carried one.
ALLOWED_HANDWRITTEN = {"segment.nf", "aggregate_size_logs.nf"}


def test_no_module_hand_writes_a_versions_heredoc():
    offenders = []
    for nf in MODULES:
        if nf.name in ALLOWED_HANDWRITTEN:
            continue
        text = nf.read_text()
        if "END_VERSIONS" in text and "ProcessEnvelope." not in text:
            offenders.append(nf.name)
    assert not offenders, (
        f"{offenders} hand-write a versions.yml heredoc. Use "
        "ProcessEnvelope.versions()/versionsStub() so the script: and stub: copies "
        "cannot name different tools."
    )


def test_script_and_stub_ask_for_the_same_tool_list():
    """The failure this whole refactor exists to prevent: two lists, one invisible."""
    mismatches = []
    for nf in MODULES:
        text = nf.read_text()
        script_calls = re.findall(r"ProcessEnvelope\.versions\(\s*task\.process\s*,\s*(\[[^\]]*\])", text)
        stub_calls = re.findall(r"ProcessEnvelope\.versionsStub\(\s*task\.process\s*,\s*(\[[^\]]*\])", text)
        if not script_calls and not stub_calls:
            continue

        def norm(xs):
            return [re.sub(r"\s+", "", x) for x in xs]

        if norm(script_calls) != norm(stub_calls):
            mismatches.append((nf.name, script_calls, stub_calls))
    assert not mismatches, (
        f"script: and stub: pass different tool lists in {[m[0] for m in mismatches]}: {mismatches}"
    )
