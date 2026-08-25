"""Meta-test for the shared Nextflow source model.

Each test here plants a construct that historically defeated a private
regex parse, and asserts the model still sees the code that follows it.
A guard built on this model is only as trustworthy as this file.
"""
from tests.nfmodel import (
    REPO_ROOT,
    block_extent,
    nf_files,
    param_refs,
    processes,
    script_bodies,
    strip_comments,
    strip_comments_and_strings,
    with_name_blocks,
)


def test_stage_as_glob_does_not_open_a_block_comment():
    """`stageAs: 'ref/*'` contains `/*`. A DOTALL regex reads that as an open
    block comment and eats everything to the next `*/` -- 64 lines of
    register.nf, including its whole script: block. The string-aware walk must
    treat it as a string literal."""
    src = (
        "process REGISTER {\n"
        "    input:\n"
        "    tuple val(meta), path(reference, stageAs: 'ref/*'), path(pre)\n"
        "\n"
        "    output:\n"
        "    path 'versions.yml', emit: versions\n"
        "\n"
        "    script:\n"
        "    MARKER_MUST_SURVIVE\n"
        "}\n"
    )
    clean = strip_comments_and_strings(src)
    assert "MARKER_MUST_SURVIVE" in clean
    assert "script:" in clean
    assert "emit: versions" in clean


def test_line_numbers_and_length_are_preserved():
    """Guards report file:line. Blanking must not shift a single character."""
    src = "a\n// comment\n'string'\n/* block */\nb\n"
    clean = strip_comments_and_strings(src)
    assert len(clean) == len(src)
    assert clean.count("\n") == src.count("\n")
    assert clean.splitlines()[0] == "a"
    assert clean.splitlines()[4] == "b"


def test_comment_contents_are_blanked():
    src = "x\n// publishDir here\n/* publishDir there */\ny\n"
    clean = strip_comments_and_strings(src)
    assert "publishDir" not in clean


def test_string_contents_are_blanked():
    src = "path 'qc/*.png'\ndef s = \"groupTuple(sort: true)\"\n"
    clean = strip_comments_and_strings(src)
    assert "groupTuple" not in clean
    assert "qc" not in clean


def test_triple_quoted_script_body_is_blanked():
    """Nextflow script bodies are triple-quoted. A `//` inside one is not a
    comment, and a `'` inside one does not open a string."""
    src = 'script:\n"""\nfoo // not a comment\nbar\n"""\n'
    clean = strip_comments_and_strings(src)
    assert "not a comment" not in clean
    assert "script:" in clean


def test_triple_quote_is_matched_as_a_unit_not_three_single_quotes():
    """A `\"\"\"` delimiter must be matched to its own kind -- the next `\"\"\"`
    -- not discovered by pairing off single `"` characters one at a time.

    A parser whose single-/double-quote branch runs before its triple-quote
    special case treats the opening `\"\"\"` as a zero-width empty string (the
    first two `"` chars) immediately followed by a lone `"` that opens a
    *new* single-quoted string extending to the next literal `"` in the body.
    From there it keeps alternately pairing off whichever `"` characters
    happen to appear inside the body, blanking one span and leaking the next.

    That mispairing can happen to land back in sync by the time it reaches
    the real closing `\"\"\"` -- which is exactly what made the previous
    version of this test blind: its body (`foo // not a comment`) contains
    *zero* embedded `"` characters, an even count, so the mis-ordered walk
    re-synchronises at the close and both of that test's assertions pass by
    coincidence, indistinguishable from a correct parser (or even a
    context-free `//`-to-EOL stripper that never looks at quotes at all).

    An ODD number of embedded `"` characters removes that coincidence: the
    mis-ordered walk is still out of sync when it reaches the close, so it
    either swallows the trailing marker as part of a still-open "string" or
    leaks unblanked body words into the output. Either failure is directly
    observable, which is what makes this test an actual guard rather than a
    property that merely happens to hold today.
    """
    src = 'script:\n"""\necho "one" "two" "three\n"""\nMARKER_AFTER\n'
    clean = strip_comments_and_strings(src)
    assert "script:" in clean
    assert "MARKER_AFTER" in clean  # correct pairing closes the body here
    assert "one" not in clean  # body contents really were blanked
    assert "two" not in clean
    assert "three" not in clean


def test_escaped_quote_does_not_end_a_string():
    src = "def s = 'it\\'s fine'\nMARKER\n"
    clean = strip_comments_and_strings(src)
    assert "MARKER" in clean


def test_block_extent_walks_nested_braces():
    text = "withName: 'A' {\n  memory = { 4.GB * task.attempt }\n  cpus = 1\n}\nTAIL"
    start = text.index("{") + 1
    end = block_extent(text, start)
    assert "cpus = 1" in text[start:end]
    assert "TAIL" not in text[start:end]


def test_block_extent_ignores_braces_inside_strings():
    text = "withName: 'A' {\n  ext.args = '--x }'\n  cpus = 1\n}\nTAIL"
    start = text.index("{") + 1
    end = block_extent(text, start)
    assert "cpus = 1" in text[start:end]
    assert "TAIL" not in text[start:end]


def test_nf_files_is_recursive():
    """A non-recursive glob('*.nf') misses modules/local/sub/*.nf. One guard
    used exactly that and could not see files it claimed to cover."""
    files = nf_files()
    assert files, "no .nf files found -- the enumerator is wrong, not the repo"
    rels = {f.relative_to(REPO_ROOT).as_posix() for f in files}
    assert "modules/local/register.nf" in rels
    assert "subworkflows/local/input_check.nf" in rels
    assert "workflows/mirage.nf" in rels


def test_register_script_body_is_visible():
    """The load-bearing case: REGISTER's script: block was invisible to
    test_resume_determinism because of stageAs: 'ref/*'."""
    body = script_bodies()["REGISTER"]
    assert body.strip(), "REGISTER's script: body came back empty"
    assert len(body.splitlines()) > 10


def test_every_module_local_process_is_modelled():
    """One process per file in modules/local/ is a project convention. If the
    model finds fewer processes than files, it is silently skipping some."""
    files = [p for p in (REPO_ROOT / "modules" / "local").rglob("*.nf")]
    assert len(processes()) >= len(files)


def test_with_name_blocks_finds_the_qc_selector():
    blocks = with_name_blocks()
    assert blocks, "no withName: blocks found"
    qc = [b for b in blocks if "WARP_SEG_QC" in b.names]
    assert qc, "the QC selector block was not found"
    assert "errorStrategy" in qc[0].body


def test_with_name_blocks_ignores_a_selector_mentioned_only_in_a_comment(tmp_path):
    """`strip_comments_and_strings` blanks a string literal's delimiting
    quotes along with its contents, not just comments -- so the selector
    regex needs real quotes to match and has to run against the RAW text.
    That means a comment that merely *mentions* `withName: 'FAKE' {` (this
    repo's conf/modules.config has several, e.g. "the `withName: 'SEGMENT'`
    ext.args closure below") must be filtered back out afterwards, or every
    such comment would be counted as a real selector block."""
    conf_dir = tmp_path / "conf"
    conf_dir.mkdir()
    (conf_dir / "modules.config").write_text(
        "// see the `withName: 'FAKE' {` block below\n"
        "withName: 'REAL' {\n"
        "    cpus = 1\n"
        "}\n"
    )
    blocks = with_name_blocks(root=tmp_path)
    names = {n for b in blocks for n in b.names}
    assert names == {"REAL"}


def test_strip_comments_keeps_string_contents_but_blanks_comments():
    """View B (`strip_comments`) is for guards that must read text living
    INSIDE a quoted string literal -- e.g. `collectFile(name: 'foo.yml', sort:
    true)` or `artifactsOf(ch_artifacts, 'real_kind')`. Unlike
    `strip_comments_and_strings` (view A), it must NOT blank string contents,
    or the exact text those guards search for vanishes along with any comment
    that merely mentions it -- see
    tests/test_resume_determinism.py::test_final_qc_collects_are_sorted, whose
    two assertions need this."""
    src = (
        "// artifactsOf(ch_artifacts, 'fake_kind').collect() -- just a comment\n"
        "artifactsOf(ch_artifacts, 'real_kind').collect(sort: true)\n"
    )
    clean = strip_comments(src)
    assert "fake_kind" not in clean  # comment content is gone
    assert "real_kind" in clean  # string literal content SURVIVES
    assert "artifactsOf(ch_artifacts, 'real_kind').collect(sort: true)" in clean


def test_strip_comments_does_not_open_a_fake_block_comment_on_a_glob():
    """View B must not re-derive a naive comment parse: it has to share the
    exact same `skip_non_code` boundary walk as view A, so a `stageAs:
    'ref/*'` glob's `/*` cannot be misread as an opening block comment here
    either -- the one and only place that decision is allowed to live."""
    src = "path(reference, stageAs: 'ref/*'), path(pre)\nscript:\nMARKER_MUST_SURVIVE\n"
    clean = strip_comments(src)
    assert "stageAs: 'ref/*'" in clean
    assert "script:" in clean
    assert "MARKER_MUST_SURVIVE" in clean


def test_param_refs_excludes_map_method_calls():
    assert param_refs("params.foo + params.bar") == {"foo", "bar"}
    assert param_refs("params.subMap(['a'])") == set()
    assert param_refs("// params.commented") == {"commented"}  # caller strips first
