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


def _sweep_files() -> list[Path]:
    """_callers() plus modules/ — a re-scattered path template is just as wrong in a
    process definition, and modules/ was outside the original sweep."""
    files = _callers() + sorted((ROOT / "modules").rglob("*.nf"))
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
    # Broadened after review: `${meta.patient_id}` was only one of the spellings a
    # copy could use. Any `${...outdir}/${<anything>}` is a per-patient template.
    template = re.compile(r"\$\{params\.(?:prior_)?outdir\}/\$\{[^}]+\}")
    offenders = [
        f"{f.relative_to(ROOT)}:{i}"
        for f in _sweep_files()
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
        for f in _sweep_files()
        for i, line in enumerate(f.read_text().splitlines(), 1)
        if literal.search(line) and not line.lstrip().startswith(("*", "//"))
    ]
    assert not offenders, (
        "checkpoint-CSV path restated outside lib/Layout.groovy: "
        + ", ".join(offenders)
        + " — call Layout.checkpointCsv/checkpointCsvRelative instead"
    )


def test_no_caller_reimplements_the_work_hash_heuristic():
    """Recognising a Nextflow task directory happens once, in Layout.isTaskDir.

    The docstring this replaces said "the 32-hex-char test" — which was the bug, not
    the rule: a task directory is <work>/<2 hex>/<30 hex>, so a 32-wide test never
    fires. A comment asserting 32 is how the defect survived its first review; the
    guard below therefore matches BOTH widths, so a copy of either the broken or the
    fixed spelling is caught.
    """
    # Broadened after review: the first version matched only the exact spelling
    # this repo happened to use. It now catches any hex character class of a
    # work-hash width, in either order, and any 30/32 length comparison, across
    # modules/ as well (see _sweep_files).
    hash_test = re.compile(
        r"\[(?:0-9a-f|a-f0-9|0-9A-Fa-f)\]\s*\{\s*3[02]"     # [0-9a-f]{32} and friends
        r"|\.\s*(?:length\(\)|size\(\))\s*==\s*3[02]"      # .length() == 32
        r"|==\s*3[02]\s*&&.*(?:hex|hash)"                     # == 32 && ...hash
    )
    offenders = [
        f"{f.relative_to(ROOT)}:{i}"
        for f in _sweep_files()
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
        r"Layout\.(?:publishedPath|patientDir)\("
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


def test_the_unregistered_slide_path_has_its_own_owner():
    """A slide that was never registered is published by whoever produced it, not
    into <pid>/registered/. The checkpoint writer must ask Layout which case it is
    in rather than assuming — that assumption is defect (c).

    The writer moved out of registration.nf into its own subworkflow so the
    add_cycle path could share it (an add_cycle run used to write no
    csv/registered.csv at all). The property is unchanged; only its owner moved."""
    writer = (
        ROOT / "subworkflows" / "local" / "registered_checkpoint.nf"
    ).read_text()
    assert "Layout.passthroughPath(" in writer, (
        "the registered-checkpoint writer no longer routes unregistered slides "
        "through Layout.passthroughPath — see tests/checkpoint_manifest.nf.test"
    )
    assert "is_passthrough" in writer

    # Both producers of an unregistered slide must set the flag the writer keys on:
    # REGISTER_PATIENT's single-slide branch, and TILED_ADAPTER's every-patient
    # reference. (The single-slide branch moved out of registration.nf with the rest
    # of the shared registration core — add_cycle.nf had lost it entirely.)
    reg = (ROOT / "subworkflows" / "local" / "register_patient.nf").read_text()
    assert "is_passthrough" in reg, (
        "REGISTER_PATIENT's single-slide passthrough branch no longer marks the "
        "slide — its checkpoint row would name a file nothing publishes"
    )
    tiled = (ROOT / "subworkflows" / "local" / "adapters" / "tiled_adapter.nf").read_text()
    assert "is_passthrough" in tiled, (
        "TILED_ADAPTER's reference passes through unregistered but is not marked "
        "is_passthrough — its registered.csv row would name a file nothing publishes"
    )


def _per_patient_publish_leaves() -> set[str]:
    """Every per-patient publish leaf conf/modules.config declares.

    Matches `path: { "${params.outdir}/${meta.patient_id}/<leaf>" }`. Multi-level leaves
    (qc/registration, cell_properties/nuclei, registered/summary, ...) are QC and producer
    sub-paths beneath a kind, not kinds themselves — only the first segment counts, and the
    QC subtree is excluded entirely because no checkpoint CSV names it.
    """
    text = MODULES_CONFIG.read_text()
    leaves = set()
    for m in re.finditer(
        r'\$\{params\.outdir\}/\$\{meta\.patient_id\}/([A-Za-z0-9_/]+)', text
    ):
        first = m.group(1).split("/")[0]
        if first == "qc":
            continue
        leaves.add(first)
    return leaves


def _declared_kinds() -> set[str]:
    """Layout.PUBLISHED_KINDS, parsed from the Groovy source."""
    text = LAYOUT.read_text()
    block = re.search(
        r"PUBLISHED_KINDS\s*=\s*\[(.*?)\]\.asImmutable\(\)", text, re.S
    )
    assert block, "Layout.PUBLISHED_KINDS not found — did the constant move?"
    body = block.group(1)
    kinds = set(re.findall(r"'([^']+)'", body))
    # Bare constant references (PREPROCESSED, REGISTERED) resolve to their own literals.
    for const in re.findall(r"^\s*([A-Z_]+),\s*$", body, re.M):
        lit = re.search(rf"static final String {const}\s*=\s*'([^']+)'", text)
        assert lit, f"Layout.PUBLISHED_KINDS names {const}, which has no String constant"
        kinds.add(lit.group(1))
    return kinds


def test_every_per_patient_publish_leaf_is_a_declared_kind():
    """The reverse direction: a leaf the config publishes into that Layout never names is
    a published path with no owner — exactly what Layout exists to prevent."""
    undeclared = _per_patient_publish_leaves() - _declared_kinds()
    assert not undeclared, (
        f"conf/modules.config publishes per-patient into {sorted(undeclared)}, which "
        "Layout.PUBLISHED_KINDS does not declare. Add them there, or stop publishing there."
    )


def test_no_declared_kind_is_dead():
    """A kind Layout declares that nothing publishes into is a path that cannot exist."""
    dead = _declared_kinds() - _per_patient_publish_leaves()
    assert not dead, (
        f"Layout.PUBLISHED_KINDS declares {sorted(dead)}, which conf/modules.config never "
        "publishes a per-patient artifact into."
    )


def _skip_non_code(text: str, i: int) -> int:
    """If `text[i:]` opens a `//` line comment, a `/* */` block comment, or a quoted
    string literal (single, double, or Groovy triple-quoted), return the index just
    past it. Otherwise return `i` unchanged.

    Shared by the block-extent walk and the comment/string stripper below, so a `{`
    or `}` — or the word `publishDir` — appearing inside a comment or a GString's
    `${...}` interpolation is treated as non-code exactly once, the same way, by
    both.
    """
    n = len(text)
    if text[i : i + 2] == "//":
        j = text.find("\n", i)
        return j if j != -1 else n
    if text[i : i + 2] == "/*":
        j = text.find("*/", i + 2)
        return j + 2 if j != -1 else n
    if text[i] in ("'", '"'):
        quote = text[i]
        if text[i : i + 3] == quote * 3:
            j = text.find(quote * 3, i + 3)
            return j + 3 if j != -1 else n
        j = i + 1
        while j < n and text[j] != quote:
            j += 2 if text[j] == "\\" else 1
        return min(j + 1, n)
    return i


def _block_extent(text: str, start: int) -> int:
    """Given `start` just past a block's opening `{`, return the index of the
    matching closing `}` — walking depth-first while skipping over comments and
    string literals, so a stray `{`/`}` inside either cannot mis-balance the walk.

    A naive character-by-character brace count treats a `{` inside an ordinary
    line comment (e.g. "note the closure syntax is { x -> ...") as real nesting,
    which makes the walk run past the block's own closing brace and silently
    borrow the NEXT block's publishDir. conf/modules.config has three places that
    mention publishDir in prose; this is why the walk must be comment-aware rather
    than merely finding *some* balanced brace span.
    """
    n = len(text)
    depth, i = 1, start
    while i < n and depth:
        skip_to = _skip_non_code(text, i)
        if skip_to != i:
            i = skip_to
            continue
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return i


def _strip_comments_and_strings(text: str) -> str:
    """Blank `//`/`/* */` comments and quoted-string contents to spaces (preserving
    newlines), so a `re.M`-anchored search over the result cannot match text that
    only appears in a comment or inside a string literal.

    Comments are the concrete failure this exists to prevent: a block that merely
    *mentions* `publishDir` in a `// TODO: decide a publishDir for this one later`
    comment previously satisfied `"publishDir" in block` — a substring check with no
    idea what a comment is — and was silently counted as routed.
    """
    out = []
    i, n = 0, len(text)
    while i < n:
        skip_to = _skip_non_code(text, i)
        if skip_to != i:
            out.append(re.sub(r"[^\n]", " ", text[i:skip_to]))
            i = skip_to
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def test_no_process_relies_on_the_fall_through_publish_default():
    """Every process in modules/local/ must have a withName: block that sets publishDir.

    The fall-through default publishes to _UNROUTED_PUBLISH/. It used to publish to a real
    run-level path, which is how SPLIT_CHANNELS came to overwrite one patient's channels
    with another's on every multi-patient run without failing.
    """
    config = MODULES_CONFIG.read_text()
    processes = set()
    for nf in sorted((ROOT / "modules" / "local").glob("*.nf")):
        m = re.search(r"^process\s+([A-Z][A-Z0-9_]*)\s*\{", nf.read_text(), re.M)
        if m:
            processes.add(m.group(1))

    # Which process names appear in a withName: selector whose block sets publishDir.
    # The block's own extent is found by a comment/string-aware brace walk (see
    # _block_extent), and "sets publishDir" means a real top-level assignment in the
    # comment/string-stripped block body, not the word appearing anywhere at all —
    # both of which conf/modules.config's own publishDir-referencing comments would
    # otherwise defeat.
    routed = set()
    for m in re.finditer(r"withName:\s*'([^']+)'\s*\{", config):
        names = m.group(1).split("|")
        start = m.end()
        end = _block_extent(config, start)
        block_clean = _strip_comments_and_strings(config[start:end])
        if re.search(r"^\s*publishDir\s*=", block_clean, re.M):
            routed.update(n.strip() for n in names)

    unrouted = processes - routed
    assert not unrouted, (
        f"{sorted(unrouted)} have no withName: block setting publishDir, so they fall "
        "through to the _UNROUTED_PUBLISH default. Give each an explicit publishDir "
        "(or `publishDir = [ enabled: false ]` if its output is intentionally not "
        "published)."
    )


def test_every_publishdir_path_closure_matches_the_literal_shape_the_parser_reads():
    """_per_patient_publish_leaves() understands exactly one shape: a `path:` closure
    that interpolates `${params.outdir}/${meta.patient_id}/<literal segments>`
    directly. A closure that reaches the same directory through a local variable,
    string concatenation, or any other Groovy expression is invisible to that regex —
    conf/modules.config could publish into an undeclared leaf and
    test_every_per_patient_publish_leaf_is_a_declared_kind would stay green, which is
    exactly the failure that test exists to catch.

    This targets any `path:` closure that reaches for the patient ID at all (the
    `patient_id` substring, not just the literal `meta.patient_id` spelling) and
    requires the WHOLE closure to be in the one shape the parser can read — so an
    unreadable shape fails loudly here instead of silently escaping the
    reverse-direction check two tests up.
    """
    config = MODULES_CONFIG.read_text()
    literal_patient_path = re.compile(
        r'^\s*path:\s*\{\s*"\$\{params\.outdir\}/\$\{meta\.patient_id\}'
        r'(?:/[A-Za-z0-9_/]*)?"\s*\}\s*,?\s*$'
    )
    offenders = []
    for i, line in enumerate(config.splitlines(), 1):
        stripped = line.strip()
        if not stripped.startswith("path:") or "patient_id" not in stripped:
            continue
        if not literal_patient_path.match(line):
            offenders.append(f"conf/modules.config:{i}: {stripped}")
    assert offenders == [], (
        "publishDir path: closure(s) reach for the patient ID in a shape "
        "_per_patient_publish_leaves() cannot parse — Layout's kind-agreement check "
        "cannot see these paths at all:\n" + "\n".join(offenders)
    )


def test_task_dir_test_is_structural_not_a_length_guess():
    """The predecessor of Layout.isTaskDir tested `length() == 32`. A Nextflow task
    directory is `<work>/<2 hex>/<30 hex>` — 30, not 32 — so it never fired. Pin the
    two properties that make the replacement correct: a 30-wide-or-more hex name AND
    a two-hex-character parent."""
    text = LAYOUT.read_text()
    body = text[text.index("static boolean isTaskDir(") :]
    body = body[: body.index("\n    }")]
    assert "{30,32}" in body, "isTaskDir must accept a 30-character task-dir name"
    assert "{2}" in body, "isTaskDir must also require the two-hex-character parent"
