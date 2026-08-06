#!/usr/bin/env python3
"""Single-ownership guards for lib/Layout.groovy — the output-layout seam.

Where a published file lands used to be stated in six independent places (a
checkpoint-CSV path template per subworkflow, a work-hash heuristic in
registration.nf, and `csv/registered.csv` / `csv/postprocessed.csv` as literals in
both ParamUtils and add_cycle.nf). Nothing tied them together, so they were kept in
agreement by eye — and the registration one had drifted into reverse-engineering
conf/modules.config at runtime.

`lib/Layout.groovy` is now the single owner. What Layout CONSTRUCTS is asserted for
real by tests/layout.nf.test (a stub pipeline run, comparing the checkpoint CSVs
byte-for-byte against the expected paths). What this file asserts is the property
that makes being single meaningful: that nobody has quietly re-scattered a path
template alongside it, and that the `kind`s Layout is asked for are ones
conf/modules.config actually publishes into.

These run in the plain pytest job — no Nextflow, no container engine — so the
single-ownership property holds even when the nf-test suite is skipped.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAYOUT = ROOT / "lib" / "Layout.groovy"
MODULES_CONFIG = ROOT / "conf" / "modules.config"


# Every Groovy/Nextflow file that could restate the layout. Layout.groovy itself is
# the one place allowed to, and conf/modules.config is the publish authority Layout
# mirrors — both are excluded from the "must not restate it" sweep.
def _callers() -> list[Path]:
    files = [ROOT / "main.nf"]
    files += sorted((ROOT / "workflows").rglob("*.nf"))
    files += sorted((ROOT / "subworkflows").rglob("*.nf"))
    files += sorted(p for p in (ROOT / "lib").glob("*.groovy") if p != LAYOUT)
    return [f for f in files if f.exists()]


def test_layout_exists_and_is_all_static():
    """G4: lib/*.groovy classes hold only static methods — no instance state."""
    text = LAYOUT.read_text()
    assert "class Layout {" in text

    # Every method declaration inside the class must be static. Matches `<mods> Type
    # name(` at method-declaration indentation, skipping the `class` line itself.
    methods = re.findall(
        r"^    ((?:private |protected |public )?)(\w[\w<>,. \[\]]*?) (\w+)\(",
        text,
        re.M,
    )
    assert methods, "no methods found — did the class layout change?"
    for mods, rtype, name in methods:
        decl = f"{mods}{rtype} {name}("
        assert "static" in rtype or "static" in mods, (
            f"Layout.{name} is not static: {decl}"
        )


def test_layout_never_reads_params():
    """Layout takes outdir as an argument; a static class reaching into `params`
    is untestable and unsafe from onComplete / lib contexts."""
    for i, line in enumerate(LAYOUT.read_text().splitlines(), 1):
        code = line.split("//")[0]
        if code.lstrip().startswith("*"):
            continue
        # Strip string literals: the error messages legitimately NAME params.outdir
        # as the thing the caller should pass in.
        code = re.sub(r"\"[^\"]*\"|'[^']*'", "", code)
        assert "params." not in code, (
            f"lib/Layout.groovy:{i} reads params — pass it in instead"
        )


def test_no_caller_restates_a_per_patient_published_path():
    """`<outdir>/<patient_id>/<kind>` belongs to Layout.patientDir alone."""
    # e.g. "${params.outdir}/${meta.patient_id}/registered"
    template = re.compile(r"\$\{params\.(?:prior_)?outdir\}/\$\{meta\.patient_id\}")
    offenders = [
        f"{f.relative_to(ROOT)}:{i}"
        for f in _callers()
        for i, line in enumerate(f.read_text().splitlines(), 1)
        if template.search(line)
    ]
    assert not offenders, (
        "per-patient published-path template restated outside lib/Layout.groovy: "
        + ", ".join(offenders)
        + " — call Layout.patientDir/publishedPath instead"
    )


def test_no_caller_restates_a_checkpoint_csv_path():
    """`csv/<step>.csv` belongs to Layout.checkpointCsv* alone."""
    literal = re.compile(r"""csv/(preprocessed|registered|postprocessed)\.csv""")
    offenders = [
        f"{f.relative_to(ROOT)}:{i}"
        for f in _callers()
        for i, line in enumerate(f.read_text().splitlines(), 1)
        if literal.search(line) and not line.lstrip().startswith(("*", "//"))
    ]
    assert not offenders, (
        "checkpoint-CSV path restated outside lib/Layout.groovy: "
        + ", ".join(offenders)
        + " — call Layout.checkpointCsv/checkpointCsvRelative instead"
    )


def test_no_caller_reimplements_the_work_hash_heuristic():
    """The 32-hex-char test lives once, in Layout.producerSubdir/isWorkHash."""
    hash_test = re.compile(r"\[0-9a-f\]\{32\}|length\(\)\s*==\s*32")
    offenders = [
        f"{f.relative_to(ROOT)}:{i}"
        for f in _callers()
        for i, line in enumerate(f.read_text().splitlines(), 1)
        if hash_test.search(line)
    ]
    assert not offenders, (
        "Nextflow work-hash heuristic reimplemented outside lib/Layout.groovy: "
        + ", ".join(offenders)
    )


def _layout_constants() -> dict[str, str]:
    return dict(
        re.findall(r"static final String (\w+)\s*=\s*'([^']+)'", LAYOUT.read_text())
    )


def test_checkpoint_step_constants_are_the_csv_basenames():
    consts = _layout_constants()
    assert consts.get("PREPROCESSED") == "preprocessed"
    assert consts.get("REGISTERED") == "registered"
    assert consts.get("POSTPROCESSED") == "postprocessed"
    assert consts.get("CSV_DIR") == "csv"


def test_every_kind_asked_of_layout_is_published_by_modules_config():
    """Layout describes the layout; conf/modules.config enforces it. They are kept
    in agreement by hand (routing publishDir through Layout is a separate, riskier
    change), so assert the agreement mechanically instead."""
    consts = _layout_constants()
    published = set(
        re.findall(
            r"\$\{params\.outdir\}/\$\{meta\.patient_id\}/([A-Za-z_][A-Za-z0-9_]*)",
            MODULES_CONFIG.read_text(),
        )
    )
    assert published, "no per-patient publishDir paths found in conf/modules.config"

    call = re.compile(
        r"Layout\.(?:publishedPath|publishedPathWithProducerSubdir|patientDir)\("
        r"\s*params\.\w+\s*,\s*[^,]+,\s*(?:'([^']+)'|Layout\.(\w+))"
    )
    asked = []
    for f in _callers():
        text = f.read_text()
        # Scanned over the whole file, not line by line: registration.nf's call is
        # wrapped across two lines and a per-line scan would silently skip it.
        for m in call.finditer(text):
            kind = m.group(1) or consts.get(m.group(2))
            line_no = text[: m.start()].count("\n") + 1
            asked.append((f"{f.relative_to(ROOT)}:{line_no}", kind))

    assert asked, "no Layout published-path calls found — did the call sites move?"
    # The two kinds whose loss would break a restart; pinned by name so a call site
    # silently disappearing is a failure rather than a shrinking list.
    assert {"registered", "preprocessed"} <= {kind for _where, kind in asked}
    unpublished = [(where, kind) for where, kind in asked if kind not in published]
    assert not unpublished, (
        "Layout is asked for a `kind` conf/modules.config never publishes into: "
        + ", ".join(f"{where} -> {kind!r}" for where, kind in unpublished)
    )
