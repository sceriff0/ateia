"""Meta-test for the shared Nextflow source model.

Each test here plants a construct that historically defeated a private
regex parse, and asserts the model still sees the code that follows it.
A guard built on this model is only as trustworthy as this file.
"""
from tests.nfmodel import block_extent, strip_comments_and_strings


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
