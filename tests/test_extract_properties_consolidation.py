#!/usr/bin/env python3
"""Guards for the one-process/two-aliases shape of EXTRACT_PROPERTIES.

`modules/local/extract_cell_properties.nf` and
`modules/local/extract_nuclei_properties.nf` used to be two files differing in
exactly six mechanical ways: the process name, one extra input, the
`--reference_mask` flag, a `nuclei/` versus `.` output prefix, the `input_bytes`
logging variable, and the stub's subdirectory. Same container, same entrypoint
script (`bin/extract_cell_properties.py`, which the nuclei process re-used with
`--reference_mask`), same emit shape. That is a parameter, not a second file.

They are now ONE process, `EXTRACT_PROPERTIES`, invoked under two Nextflow
aliases -- an alias is not cosmetic here, it is required: DSL2 refuses to call
the same process twice in one workflow, and `subworkflows/local/segmentation.nf`
calls it twice.

The second, more interesting property this file pins is the COUPLING DIRECTION.
The old nuclei module's own comment said its mandatory `mkdir -p nuclei` existed
*only* so that `Layout.publishedPath`'s `producerSubdir` heuristic -- a
downstream, consumer-side string test over task-directory names -- could recover
the `'nuclei'` segment afterwards. The producer's on-disk layout was dictated by
a consumer's introspection. Now the caller NAMES the subdirectory
(`Layout.NUCLEI_SUBDIR`), hands it to the producer as an input, and hands the
same value to `Layout.publishedPath`'s explicit-subdir overload when it records
the checkpoint row. Nothing about the cell_properties artifacts is inferred from
directory shape any more.

`producerSubdir` itself SURVIVES -- REGISTER's `registered_slides/`,
TILED_STITCH's `registered/` and EXPORT_GEOJSON's `export/` still reach Layout
through the inferring 4-argument overload, and `publishedOrAsIs` is built on it.
Removing it repo-wide is a different, larger change. What is asserted here is
only that the cell_properties pair no longer depends on it.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MODULE = ROOT / "modules" / "local" / "extract_properties.nf"
OLD_MODULES = [
    ROOT / "modules" / "local" / "extract_cell_properties.nf",
    ROOT / "modules" / "local" / "extract_nuclei_properties.nf",
]
LAYOUT = ROOT / "lib" / "Layout.groovy"
SEGMENTATION = ROOT / "subworkflows" / "local" / "segmentation.nf"
ADD_CYCLE = ROOT / "subworkflows" / "local" / "add_cycle.nf"
SENTINEL = ROOT / "assets" / "NO_REFERENCE_MASK"

CALL_SITES = [SEGMENTATION, ADD_CYCLE]

# `//` line comments and `/* */` block comments. Groovy/Nextflow only -- a `#`
# inside a process's triple-quoted shell body is NOT a Groovy comment and is
# deliberately left in place, so a module that writes the subdirectory name into
# a shell comment is still caught by the single-ownership sweep below.
COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)


def _strip_comments(text: str) -> str:
    """Blank comments to spaces, preserving newlines so line numbers survive."""
    return COMMENT_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


def _code_files() -> list[Path]:
    """Every Groovy/Nextflow/config file that could restate the subdirectory.

    lib/Layout.groovy is excluded: it is the owner. tests/ is excluded because a
    test asserting the published layout must be free to spell it out.
    """
    files: list[Path] = [ROOT / "main.nf"]
    files += sorted((ROOT / "modules").rglob("*.nf"))
    files += sorted((ROOT / "subworkflows").rglob("*.nf"))
    files += sorted((ROOT / "workflows").rglob("*.nf"))
    files += sorted(p for p in (ROOT / "lib").glob("*.groovy") if p != LAYOUT)
    files += sorted((ROOT / "conf").glob("*.config"))
    return [f for f in files if f.exists()]


def test_the_sweep_can_see_a_restated_subdirectory():
    """Regression guard on the sweep itself.

    `_strip_comments` is what lets the single-ownership test below tolerate prose
    that mentions the subdirectory. A stripper that blanked too much -- or a
    pattern anchored to one spelling -- would make that test unable to fail,
    which is this repo's signature defect. Exercise both directions on synthetic
    text so the guard is proven live even when the tree is clean.
    """
    assert _strip_comments("// writes into nuclei/ so\ncode 'nuclei'\n").split() == [
        "code",
        "'nuclei'",
    ]
    assert _SUBDIR_LITERAL_RE.search("path(\"nuclei/contours.json\")")
    assert _SUBDIR_LITERAL_RE.search("def x = 'nuclei'")
    assert _SUBDIR_LITERAL_RE.search('def x = "nuclei"')
    # Must NOT fire on the many legitimate `nuclei_mask` spellings.
    assert not _SUBDIR_LITERAL_RE.search("tuple val(meta), path(nuclei_mask)")
    assert not _SUBDIR_LITERAL_RE.search("nuclei_mask: 'x'")


# A restated subdirectory: the quoted bare word, or the path segment `nuclei/`.
# `nuclei_mask` and friends must not match, so the bare-word alternatives are
# bounded on both sides.
_SUBDIR_LITERAL_RE = re.compile(r"""(?<![\w])nuclei/|'nuclei'|"nuclei\"""")


def test_one_module_file_owns_both_property_extractions():
    assert MODULE.exists(), (
        f"{MODULE.relative_to(ROOT)} does not exist -- the cell and nucleus "
        "property extractions are meant to be one parameterised process."
    )
    names = re.findall(r"^process\s+(\w+)\s*\{", MODULE.read_text(), re.M)
    assert names == ["EXTRACT_PROPERTIES"], (
        f"{MODULE.relative_to(ROOT)} declares {names}, expected exactly "
        "['EXTRACT_PROPERTIES']"
    )
    still_there = [p.relative_to(ROOT) for p in OLD_MODULES if p.exists()]
    assert not still_there, (
        f"{still_there} still exist alongside {MODULE.relative_to(ROOT)} -- the "
        "consolidation left the originals behind, so two definitions can drift."
    )


def test_both_call_sites_alias_the_one_process():
    """Every caller reaches the merged module, under both historical names.

    The alias names are load-bearing beyond readability: `conf/modules.config`'s
    `withName:` selector, the `task.process` written into each size-log row, and
    the per-alias `*.size.csv` filename all key on them.
    """
    problems = []
    for f in CALL_SITES:
        text = f.read_text()
        for alias in ("EXTRACT_CELL_PROPERTIES", "EXTRACT_NUCLEI_PROPERTIES"):
            if not re.search(
                rf"include\s*\{{[^}}]*EXTRACT_PROPERTIES\s+as\s+{alias}[^}}]*\}}"
                r"\s*from\s*'[^']*extract_properties'",
                text,
                re.S,
            ):
                problems.append(
                    f"{f.relative_to(ROOT)}: no `include {{ EXTRACT_PROPERTIES as "
                    f"{alias} }} from '.../extract_properties'`"
                )
        for old in ("extract_cell_properties'", "extract_nuclei_properties'"):
            if old in text:
                problems.append(
                    f"{f.relative_to(ROOT)}: still includes from '{old[:-1]}'"
                )
    assert not problems, "\n".join(problems)


def test_the_nuclei_subdirectory_is_named_once_in_layout():
    """`lib/Layout.groovy` owns the published layout, subdirectory included.

    The subdirectory IS part of a published path (`<pid>/cell_properties/nuclei/`),
    so restating it in a module, a subworkflow or conf/modules.config is the same
    defect `tests/test_layout.py` forbids for the rest of the path.
    """
    layout = LAYOUT.read_text()
    assert re.search(
        r"static final String NUCLEI_SUBDIR\s*=\s*'nuclei'", layout
    ), (
        "lib/Layout.groovy does not declare NUCLEI_SUBDIR = 'nuclei' -- the "
        "subdirectory segment of a published path has no owner."
    )

    offenders = []
    for f in _code_files():
        code = _strip_comments(f.read_text())
        for i, line in enumerate(code.splitlines(), 1):
            if _SUBDIR_LITERAL_RE.search(line):
                offenders.append(f"{f.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not offenders, (
        "the nucleus output subdirectory is restated outside lib/Layout.groovy:\n"
        + "\n".join(offenders)
        + "\n-- reference Layout.NUCLEI_SUBDIR instead."
    )


def test_layout_can_be_told_the_subdirectory_instead_of_inferring_it():
    """The reversed coupling: a consumer is TOLD the producer subdirectory.

    `Layout.publishedPath(outdir, pid, kind, file)` infers the subdirectory from
    the file's parent directory name (`producerSubdir`). That inference is what
    forced the old nuclei module to write into `nuclei/` -- a producer contorted
    to make a downstream string heuristic work. The five-argument overload takes
    the subdirectory as a fact, and segmentation.nf passes the same value it
    passed the producer.
    """
    layout = LAYOUT.read_text()
    assert re.search(
        r"static String publishedPath\(\s*def outdir,\s*def patientId,"
        r"\s*String kind,\s*String subdir,\s*def file\s*\)",
        layout,
    ), (
        "lib/Layout.groovy has no explicit-subdir publishedPath overload -- every "
        "caller is still forced to let producerSubdir guess."
    )

    text = SEGMENTATION.read_text()
    calls = re.findall(
        r"Layout\.publishedPath\((?:[^()]|\([^()]*\))*\)", text
    )
    cell_calls = [c for c in calls if "cell_properties" in c]
    assert len(cell_calls) == 2, (
        "expected exactly two cell_properties published-path calls in "
        f"{SEGMENTATION.relative_to(ROOT)} (cell contours + nucleus contours), "
        f"found {len(cell_calls)}: {cell_calls}"
    )
    inferring = [c for c in cell_calls if len(_split_args(c)) != 5]
    assert not inferring, (
        "cell_properties published paths still let producerSubdir infer the "
        f"subdirectory: {inferring} -- pass it explicitly."
    )
    assert any("Layout.NUCLEI_SUBDIR" in c for c in cell_calls), (
        "neither cell_properties call names Layout.NUCLEI_SUBDIR, so the nucleus "
        f"row's subdirectory is coming from somewhere else: {cell_calls}"
    )


def _split_args(call: str) -> list[str]:
    """Split a single-level Groovy call's argument list on top-level commas."""
    inner = call[call.index("(") + 1 : call.rindex(")")]
    args, depth, cur = [], 0, ""
    for ch in inner:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            args.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        args.append(cur.strip())
    return args


def test_split_args_counts_a_nested_call_as_one_argument():
    """Regression guard on the splitter above: a naive `.split(',')` would count
    `Layout.publishedPath(a, b, 'k', s, f(x, y))` as six arguments and let a
    four-argument (inferring) call masquerade as five."""
    assert _split_args("F(a, b, 'k', s, g(x, y))") == ["a", "b", "'k'", "s", "g(x, y)"]
    assert _split_args("F(a, b, 'k', f)") == ["a", "b", "'k'", "f"]


def test_the_reference_mask_is_a_sentinel_not_an_optional_input():
    """One input arity for both invocations, the way QUANTIFY does REDSEA.

    `modules/local/quantify.nf` already stages `assets/NO_REDSEA` and gates
    `--redsea-geometry` on the filename. Re-using that idiom keeps the channel
    graph free of an optional-input branch and keeps the process's cache key off
    `params`.
    """
    assert SENTINEL.exists(), (
        "assets/NO_REFERENCE_MASK is missing -- the cell invocation has nothing "
        "to stage in the reference_mask slot."
    )
    module = MODULE.read_text()
    assert "reference_mask.name == 'NO_REFERENCE_MASK'" in module, (
        "modules/local/extract_properties.nf does not gate --reference_mask on "
        "the sentinel filename."
    )
    assert "--reference_mask" in module

    for f in CALL_SITES:
        assert "NO_REFERENCE_MASK" in f.read_text(), (
            f"{f.relative_to(ROOT)} never stages the sentinel, so its cell "
            "invocation cannot fill the reference_mask input slot."
        )


def test_the_published_size_log_keeps_its_per_alias_filename():
    """`<pid>.EXTRACT_CELL_PROPERTIES.size.csv` / `...NUCLEI...` are PUBLISHED
    filenames, so the merge must not rename them. One process cannot hardcode two
    names; it derives the leaf from `task.process`, which carries the alias."""
    module = MODULE.read_text()
    assert "task.process.tokenize(':').last()" in module, (
        "modules/local/extract_properties.nf does not derive its size-log name "
        "from the invoking alias -- both aliases would publish the same filename, "
        "renaming one of two published artifacts."
    )
    # Both the script: and the stub: block need it -- a stub run publishes the
    # size log too, and the stub gate is the one CI always runs.
    assert module.count("task.process.tokenize(':').last()") >= 2, (
        "only one of the script:/stub: blocks derives the per-alias size-log name"
    )
