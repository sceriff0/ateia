"""The committed format-validation report must be evidence, not a placeholder.

RULING R3 trades the vendor FIXTURES for this file. That trade only holds while
the file is a real measurement of a real run -- a template, or a report whose
rows all failed, is strictly worse than an honest "untested", because it reads
as coverage.

So: it must name the commit it was produced from, and it must carry a row for
every vendor format the pipeline claims. What it must NOT do is assert every row
is OK: a recorded failure is a finding, and a guard that forbade it would create
a reason to delete the row instead of fixing the reader.

PENDING IS A LEGAL, EXPLICIT STATE. Task 13's cluster kit needs a cluster and
private images that this repository's CI never has, so this phase ships with
the kit built and the evidence outstanding rather than either omitting the page
(the exit-state table names it) or faking a run that never happened. The report
declares that state with the literal marker `kit-validated: pending` -- present,
it relaxes only the checks that need a real run to have happened (the commit
SHA, the probe container, at least one successful row) and ADDS a check that the
pending page cannot itself fake a success. It never relaxes the checks that hold
regardless of whether the kit has run: every vendor format must still have a
row, and the template's placeholder path must still never appear.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "docs" / "validation" / "format_validation.md"

# The formats that CANNOT be synthesised, i.e. the ones this report exists for.
# Everything else is covered on every push by tests/integration/formats/.
VENDOR_FORMATS = (".czi", ".nd2", ".lif", ".ndpi", ".svs")

# The exact string the pending page carries instead of a real measurement.
# tests/cluster/validate_formats.sh's OWN output never contains this string --
# it always writes a real `pipeline commit: `<sha>`` header -- so its presence
# is unambiguous evidence that no cluster run has landed yet.
PENDING_MARKER = "kit-validated: pending"


def _text() -> str:
    assert REPORT.is_file(), (
        f"{REPORT.relative_to(ROOT)} is missing. It is produced by "
        "`tests/cluster/validate_formats.sh` on the cluster and committed as the "
        "evidence RULING R3 accepts in place of vendor fixtures -- or, before that "
        f"run has happened, as an explicit pending page carrying '{PENDING_MARKER}'."
    )
    return REPORT.read_text(encoding="utf-8")


def _is_pending(text: str) -> bool:
    return PENDING_MARKER in text


def test_the_report_declares_a_real_run_or_an_explicit_pending_state():
    """Never silently neither. A file that is not the template, not a real run,
    and not marked pending is the one shape this guard cannot tell apart from a
    hand-edited fragment -- so it must be exactly one of the two named states."""
    text = _text()
    has_real_commit = re.search(r"pipeline commit:\s*`[0-9a-f]{40}`", text) is not None
    assert _is_pending(text) or has_real_commit, (
        f"the report is neither the explicit pending state (containing "
        f"'{PENDING_MARKER}') nor a real run (a 40-character commit SHA under "
        "'pipeline commit:') -- it must be one or the other, never silently neither"
    )


def test_the_report_names_the_commit_it_was_produced_from():
    """Without a SHA the report cannot be tied to a tree, and a report that
    outlives the code it measured is a claim, not evidence."""
    text = _text()
    if _is_pending(text):
        pytest.skip("pending state: no cluster run has been recorded yet")
    assert re.search(r"pipeline commit:\s*`[0-9a-f]{40}`", text), (
        "no 40-character commit SHA in the report header -- validate_formats.sh "
        "writes one, so this file was hand-edited or truncated"
    )


def test_the_report_names_the_container_the_probe_ran_in():
    """`.svs` needs the Bio-Formats jars baked into the image (RULING R2). Which
    image produced these numbers is part of the measurement."""
    text = _text()
    if _is_pending(text):
        pytest.skip("pending state: no cluster run has been recorded yet")
    assert "probe container:" in text


@pytest.mark.parametrize("suffix", VENDOR_FORMATS)
def test_every_vendor_format_has_a_row(suffix):
    """Holds in BOTH states: the pending page must still list every format it is
    promising to validate, so dropping a format silently (pending or not) is
    caught the same way."""
    text = _text()
    assert suffix in text, (
        f"the report has no row for {suffix}. That is one of the five formats the "
        "README claims and CI cannot synthesise -- either it was validated and the "
        "row is missing, or it was not validated and the claim should come out of "
        "README.md and bin/convert_image.py's docstring in the same commit."
    )


def test_the_report_is_not_the_template():
    """The template's placeholder path must never reach the committed report, in
    either state."""
    assert "/absolute/path/to/your/" not in _text(), (
        "the committed report still contains the samplesheet template's placeholder "
        "path, so it records a run that never happened"
    )


def test_the_report_records_at_least_one_successful_conversion():
    """A report of nothing but failures is a real outcome and is allowed to be
    committed -- but it must not be mistaken for coverage, so at least one row has
    to be an actual success before this counts as validation."""
    text = _text()
    if _is_pending(text):
        pytest.skip("pending state: no cluster run has been recorded yet")
    assert "| OK |" in text, (
        "no row in the report succeeded. Read the FAILED rows: this is a finding "
        "about the readers, not about the report."
    )


def test_the_pending_report_does_not_fake_a_successful_row():
    """The one check the pending state ADDS rather than relaxes: `| OK |` is the
    exact string validate_formats.sh's probe writes for a real success, and its
    presence on a page that has not run the kit would read as coverage that never
    happened -- precisely what this whole guard exists to prevent."""
    text = _text()
    if not _is_pending(text):
        pytest.skip("real report: OK rows are expected once the kit has run")
    assert "| OK |" not in text, (
        "the pending report contains '| OK |' -- that claims a successful probe "
        "that never ran; remove it, or replace this file with the kit's real output "
        "and drop the pending marker"
    )
