"""A string-aware character walk over Nextflow/Groovy source.

Moved here verbatim from tests/test_layout.py's private `_skip_non_code`,
`_strip_comments_and_strings` and `_block_extent` (dropping the leading
underscore -- they are now public). See tests/test_nfmodel.py for the
specific constructs -- a `stageAs: 'ref/*'` glob that a naive `/\\*.*?\\*/`
regex reads as an open block comment, a `{` inside a comment mis-balancing a
naive brace count -- that defeated the private regex parses this replaces.
"""
import re

__all__ = ["block_extent", "skip_non_code", "strip_comments", "strip_comments_and_strings"]


def skip_non_code(text: str, i: int) -> int:
    """If `text[i:]` opens a `//` line comment, a `/* */` block comment, or a
    quoted string literal (single, double, or Groovy triple-quoted), return
    the index just past it. Otherwise return `i` unchanged.

    Shared by the block-extent walk and the comment/string stripper below, so
    a `{` or `}` -- or the word `publishDir` -- appearing inside a comment or
    a GString's `${...}` interpolation is treated as non-code exactly once,
    the same way, by both.
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


def block_extent(text: str, start: int) -> int:
    """Given `start` just past a block's opening `{`, return the index of the
    matching closing `}` -- walking depth-first while skipping over comments
    and string literals, so a stray `{`/`}` inside either cannot mis-balance
    the walk.

    A naive character-by-character brace count treats a `{` inside an
    ordinary line comment (e.g. "note the closure syntax is { x -> ...") as
    real nesting, which makes the walk run past the block's own closing brace
    and silently borrow the NEXT block's publishDir. conf/modules.config has
    three places that mention publishDir in prose; this is why the walk must
    be comment-aware rather than merely finding *some* balanced brace span.
    """
    n = len(text)
    depth, i = 1, start
    while i < n and depth:
        skip_to = skip_non_code(text, i)
        if skip_to != i:
            i = skip_to
            continue
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return i


def _strip(text: str, *, blank_strings: bool) -> str:
    """The one walk both stripping views project from. Both need the exact
    same answer to "where does a comment start" and "where does a string
    start" -- that shared answer is `skip_non_code`. The only thing that
    differs between the two public views is what happens to a string
    literal's span once `skip_non_code` has located it: view A
    (`strip_comments_and_strings`) blanks it like a comment; view B
    (`strip_comments`) keeps it verbatim. Neither view re-derives the
    comment/string boundary logic -- that would be exactly the private,
    forkable Nextflow-source parse this module exists to replace.
    """
    out = []
    i, n = 0, len(text)
    while i < n:
        skip_to = skip_non_code(text, i)
        if skip_to == i:
            out.append(text[i])
            i += 1
            continue
        span = text[i:skip_to]
        is_string = text[i] in ("'", '"')
        if is_string and not blank_strings:
            out.append(span)
        else:
            out.append(re.sub(r"[^\n]", " ", span))
        i = skip_to
    return "".join(out)


def strip_comments_and_strings(text: str) -> str:
    """Blank `//`/`/* */` comments and quoted-string contents to spaces
    (preserving newlines), so a `re.M`-anchored search over the result cannot
    match text that only appears in a comment or inside a string literal.

    Comments are the concrete failure this exists to prevent: a block that
    merely *mentions* `publishDir` in a `// TODO: decide a publishDir for
    this one later` comment previously satisfied `"publishDir" in block` -- a
    substring check with no idea what a comment is -- and was silently
    counted as routed.
    """
    return _strip(text, blank_strings=True)


def strip_comments(text: str) -> str:
    """Blank `//`/`/* */` comments only, keeping every string literal's
    contents -- quotes included -- verbatim.

    A guard that needs to see what is *inside* a string literal (a quoted
    filename or a quoted argument, e.g. `collectFile(name:
    'collated_versions.yml', sort: true)`) cannot use
    `strip_comments_and_strings`: that view blanks string contents on
    purpose, so the guard's own target text would vanish along with any
    comment mention of it, and the check would either match nothing forever
    (a permanent false failure) or, if it captures a group from inside the
    quotes, match nothing and go silently blind. This view still needs to be
    string-*aware* -- a `stageAs: 'ref/*'` glob's `/*` must not be misread as
    an opening block comment -- it just does not blank what it finds inside
    the quotes.
    """
    return _strip(text, blank_strings=False)
