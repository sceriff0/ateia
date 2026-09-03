"""An nf-test output value is a **String**, not a File. Assertions must wrap it.

`process.out.<x>.get(0).get(1)` is a `java.lang.String` holding a path (or an
`ArrayList` of them) in nf-test 0.9.0. So an assertion that reaches straight for
a `File` member gets one of three outcomes, and **none** of them is the check
its author wrote:

* `.name`, `.toFile()`, `.exists()`, `.text` -- `MissingPropertyException` /
  `MissingMethodException`. The test can never pass, whatever the pipeline does.
* `.readLines()` -- Groovy *does* define `String.readLines()`. It splits the
  path string, so a 4-line CSV silently reads as **one** line and the assertion
  fails for a reason that has nothing to do with the file.
* `.size()` -- the character count of the path. Measured on nightly run
  33633986188: `assert it.size() > 1000` reported **161**, the length of a work
  directory path, against a file that was on disk and was fine.

That run was **14 of 28 `real` cases red, and 8 of the 14 were this one bug**
(`.superpowers/sdd/01-suite-truth/nightly_real_33633986188.log`, families
tabulated in `.github/workflows/nightly.yml`'s header). The `stub`-tagged cases
sitting in the *same files* wrap with `file(it)` and were green throughout --
which is the point: nothing in the blocking suite could tell the two apart,
because `nf-test test --tag stub` never runs a `real` case at all. This guard is
that missing signal, and it runs in the blocking pytest suite.

**The rule.** Inside any `test { }` block of any `.nf.test` file, a value
derived from `process.out` / `workflow.out` by index (`.get(n)`, `[n]`) is
*tainted*. A tainted value may not receive `.name`, `.getName()`, `.toFile()`,
`.exists()`, `.text`, `.readLines()`, `.absolutePath`, `.isFile()` or
`.lastModified()`; nor `.size()`, unless that `.size()` is immediately compared
to a literal small enough to be an element count rather than a byte
threshold (see SIZE_IS_CARDINALITY_BELOW). Wrapping
in `file(...)`, `path(...)` or `new File(...)` removes the taint, because that
is exactly the conversion the assertion needs. Taint follows a `def` binding, a
`with(...)` receiver, and the parameter of `.each`/`.every`/`.collect`/`.any`/
`.find`/`.findAll`/`.sort`/`.eachWithIndex`/`.with` over a tainted value --
because that
is how seven of the eight failures were written: not as one long chain, but
through a local.

The correct fix is a wrap at the seam, once:

    def files = process.out.channels.get(0).get(1).collect { file(it) }
    assert files.size() == 3
    files.each { f -> assert f.size() > 100 }
    assert files*.name.any { it.contains('DAPI') }

**Why the model.** Parsing is delegated to `tests/nfmodel` -- `nf_test_files()`
to enumerate and `strip_comments` / `block_extent` to read -- per this repo's
rule that no guard carries a private Nextflow parse. The *string-preserving*
view is the correct one here: the thing under assertion is code, and a
GString-interpolated `"... ${f.name} ..."` inside an assertion message is code
that runs and throws just like the bare expression, so blanking string contents
would hide real hits.

**Known gap.** This guard does not catch every way a `real` case can misread an
nf-test output -- only the type-confusion class above. Two of the FOUR reds
fixed in `783cfaa` (`preprocessing.nf.test`/`registration.nf.test`'s
`3dc7c107`/`7ef417e4`) were a different bug entirely: an unqualified
`collect`/`each`/etc. called *inside* a `with(receiver) { ... }` block does not
resolve against `receiver` the way a qualified `receiver.collect { ... }` does
-- Groovy hands the closure the WHOLE enclosing scope's `it`, not the `with`
receiver, so `it[0]` inside such a closure is the first element of whatever
`it` happens to be at that call site, not an element of `receiver`. That
produced `MissingPropertyException: ... No such property: patient_id for
class: java.lang.String` where the author's intent was clearly `receiver[0]`.
Closing it statically needs delegate-scope analysis (tracking what `with`'s
receiver actually binds `it`/an implicit call to, across nested closures) that
this file's taint walk does not attempt -- the walk here tracks TYPE (String
vs File), not SCOPE (which receiver a closure call resolves against). A
one-off scan over every `with(...)` body in `tests/**/*.nf.test` at the time
found no other instance of it; a standing guard for this class remains
unwritten.
"""

import re

from tests.nfmodel import block_extent, nf_test_files, strip_comments

# Members that do not exist on java.lang.String at all, or that exist and mean
# something else entirely (`readLines`). Forbidden on a tainted value at any
# index depth.
FILE_ONLY_MEMBERS = (
    "name",
    "getName",
    "toFile",
    "exists",
    "text",
    "readLines",
    "absolutePath",
    "isFile",
    "lastModified",
)

# `size()` is the one that cannot be settled by type: on a tuple or a grouped
# list it is a CARDINALITY and legitimate (`...get(0)[1].size() == 2` -- two
# CSVs); on a path String it is the character count of the path, which is the
# defect (161, on the CI runner). Rather than guess, the guard reads the
# THRESHOLD, which is not ambiguous at all: a cardinality in these tests is
# compared to a small literal (1, 2, 3), and a byte check is compared to 100,
# 1000, 10_000 -- nobody asserts a channel holds at least a hundred elements.
# So `.size()` is accepted only when it is immediately compared to a literal
# below SIZE_IS_CARDINALITY_BELOW. Bound to a local first
# (`def maskSize = ....size()`) it has no adjacent literal and is flagged, which
# is right: that form is only ever written to interpolate a byte count into a
# failure message.
AMBIGUOUS_MEMBER = "size"
SIZE_IS_CARDINALITY_BELOW = 100
# The index depth at which an nf-test output value stops being the emitted tuple
# and starts being one of its slots: `process.out.x.get(i).get(j)`.
AMBIGUOUS_MIN_DEPTH = 2
_SIZE_COMPARISON = re.compile(r"\(\s*\)\s*(?:==|!=|>=|<=|>|<)\s*([\d_]+)")

# The three conversions that turn a path String into something with File
# members. Any of them, applied at the seam, discharges the taint.
WRAPPERS = ("file", "path", "new File")

_TEST_BLOCK = re.compile(r"\btest\s*\(")
_INDEXER = r"(?:\.get\(\s*\d+\s*\)|\[\s*\d+\s*\])"
# `process.out` / `workflow.out`, an optional emit name, then one or more index
# steps. Requiring at least one index step is what keeps `assert
# process.out.channels.size() == 1` -- the channel's own arity -- out of scope.
_OUT_CHAIN = re.compile(rf"\b(?:process|workflow)\.out\b(?:\.\w+)?((?:\s*{_INDEXER})+)")
# The same chain written relative to a `with(...)` receiver: a bare `get(0)` in
# statement position, not preceded by a dot or an identifier character.
_BARE_CHAIN = re.compile(rf"(?<![.\w])(get\(\s*\d+\s*\)(?:\s*{_INDEXER})*)")
_ITERATOR = re.compile(
    r"\.\s*(?:each|every|collect|any|find|findAll|sort|eachWithIndex|with)\s*\{"
    r"\s*(?:(\w+)\s*->)?"
)
_WITH = re.compile(r"\bwith\s*\(")
_ASSIGN = re.compile(r"(?:\bdef\s+)?\b(\w+)\s*=(?!=)")


def _depth(chain: str) -> int:
    """How many index steps a matched chain carries."""
    return len(re.findall(_INDEXER, chain))


def _is_the_meta_slot(chain: str, depth: int) -> bool:
    """True for `...get(i).get(0)` / `...[i][0]` -- element 0 of an emitted
    tuple. MOST tuple outputs in this pipeline are `tuple val(meta), path(...)`,
    so slot 0 is usually a Map, and a `.name` on it is a map lookup rather than
    the bug this guard is about -- but not every emit agrees: `register.nf`'s
    `registered` emit is `tuple val(patient_id), path(...), val(all_metas),
    path(...)`, where slot 0 is a `val` holding a plain String, so `.get(0).size()`
    there really would be the path-length bug. This heuristic is process-blind
    (it has no way to know which emit produced a given chain) and deliberately
    accepts that false negative rather than flag every legitimate meta-map
    lookup; a chain against `register.nf`'s `registered` output is not currently
    distinguished from one against a `tuple val(meta), path(...)` emit. Only
    meaningful from depth two: on a `path`-only emit, `...get(0)` IS the path."""
    if depth < AMBIGUOUS_MIN_DEPTH:
        return False
    last = re.findall(_INDEXER, chain)[-1]
    return re.search(r"\d+", last).group(0) == "0"


def _is_wrapped(text: str, start: int) -> bool:
    """True when the expression beginning at `start` sits directly inside a
    `file(`, `path(` or `new File(` call -- i.e. the taint was discharged."""
    before = text[:start].rstrip()
    return any(before.endswith(w + "(") for w in WRAPPERS)


def _matching_paren(text: str, open_idx: int) -> int:
    """Index just past the `)` matching the `(` at `open_idx`."""
    depth, i, n = 0, open_idx, len(text)
    while i < n:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _size_is_a_cardinality(text: str, after_member: int) -> bool:
    """True when a `.size()` at `after_member` is immediately compared to a
    literal small enough to be an element count rather than a byte threshold."""
    m = _SIZE_COMPARISON.match(text[after_member:])
    return bool(m) and int(m.group(1).replace("_", "")) < SIZE_IS_CARDINALITY_BELOW


def _member_after(text: str, end: int):
    """If the expression ending at `end` is immediately followed by a forbidden
    member access (including the spread form `*.name`), return its name."""
    m = re.match(r"\s*\*?\s*\.\s*(\w+)", text[end:])
    if not m:
        return None
    member = m.group(1)
    if member in FILE_ONLY_MEMBERS:
        return member
    if member == AMBIGUOUS_MEMBER and not _size_is_a_cardinality(text, end + m.end()):
        return member
    return None


def _scan(body: str, tainted: dict, problems: list, where: str, base_depth: int = -1):
    """Walk one block. `tainted` maps a local name to the index depth its value
    carries; `base_depth` >= 0 means bare `get(n)` chains in this block are
    relative to a `with(...)` receiver of that depth (and `-1` means there is no
    such receiver, so bare chains are not output-rooted)."""

    def record(start: int, end: int, expr: str):
        member = _member_after(body, end)
        if member and not _is_wrapped(body, start):
            line = body[:start].count("\n") + 1
            problems.append(
                f"{where}+{line}: `{expr.strip()}.{member}` -- that value is a "
                f"path String, not a File. Wrap it: "
                f"file({expr.strip()}).{member}"
            )

    for m in _OUT_CHAIN.finditer(body):
        if not _is_the_meta_slot(m.group(1), _depth(m.group(1))):
            record(m.start(), m.end(), m.group(0))

    if base_depth >= 0:
        for m in _BARE_CHAIN.finditer(body):
            if not _is_the_meta_slot(m.group(1), base_depth + _depth(m.group(1))):
                record(m.start(), m.end(), m.group(0))

    # Taint through a local binding: `def files = <tainted expr>`, and onward
    # through a second binding off the first (`def fileList = (files instanceof
    # List) ? files : [files]` -- the shape the stub cases use, and the one the
    # break-it check exposed as a hole on the guard's first draft). Run to a
    # fixpoint, because the bindings need not appear in dependency order. A
    # wrapper anywhere in the right-hand side discharges the taint, which is
    # what makes `.collect { file(it) }` the fix rather than a workaround.
    changed = True
    while changed:
        changed = False
        for line in body.split("\n"):
            a = _ASSIGN.search(line)
            if not a or a.group(1) in tainted:
                continue
            rhs = line[a.end() :]
            if any(w + "(" in rhs for w in WRAPPERS):
                continue
            chain = _OUT_CHAIN.search(rhs) or (
                _BARE_CHAIN.search(rhs) if base_depth >= 0 else None
            )
            if chain:
                depth = _depth(chain.group(1))
                if chain.re is _BARE_CHAIN:
                    depth += base_depth
                if _is_the_meta_slot(chain.group(1), depth):
                    continue
            elif any(re.search(rf"\b{re.escape(n)}\b", rhs) for n in tainted):
                depth = AMBIGUOUS_MIN_DEPTH
            else:
                continue
            tainted[a.group(1)] = depth
            changed = True

    for name in tainted:
        for m in re.finditer(rf"\b{re.escape(name)}\b", body):
            record(m.start(), m.end(), name)

    # Taint through an iteration closure's parameter: the parameter is a single
    # ELEMENT of a tainted collection, so any File member on it is a defect.
    receivers = list(_OUT_CHAIN.finditer(body)) + (
        list(_BARE_CHAIN.finditer(body)) if base_depth >= 0 else []
    )
    for name in tainted:
        receivers += list(re.finditer(rf"\b{re.escape(name)}\b", body))
    for r in receivers:
        it = _ITERATOR.match(body, r.end())
        if not it:
            continue
        param = it.group(1) or "it"
        end = block_extent(body, it.end())
        inner = body[it.end() : end - 1]
        for m in re.finditer(rf"\b{re.escape(param)}\b", inner):
            member = _member_after(inner, m.end())
            if member and not _is_wrapped(inner, m.start()):
                line = body[: it.end()].count("\n") + inner[: m.start()].count("\n") + 1
                problems.append(
                    f"{where}+{line}: `{param}.{member}` inside a closure over a "
                    f"raw nf-test output -- `{param}` is a path String, not a "
                    f"File. Wrap it: file({param}).{member}"
                )

    # `with(<tainted>) { ... }`: the body's bare `get(n)` chains and its `it`
    # are the receiver, so recurse with the receiver's depth as the base.
    for w in _WITH.finditer(body):
        close = _matching_paren(body, w.end() - 1)
        recv = body[w.end() : close - 1].strip()
        chain = _OUT_CHAIN.fullmatch(recv) or (
            _BARE_CHAIN.fullmatch(recv) if base_depth >= 0 else None
        )
        depth = None
        if chain:
            depth = _depth(chain.group(1)) + (
                base_depth if chain.re is _BARE_CHAIN else 0
            )
        elif recv in tainted:
            depth = tainted[recv]
        elif re.fullmatch(r"(?:process|workflow)\.out(?:\.\w+)?", recv):
            depth = 0
        if depth is None:
            continue
        brace = body.find("{", close)
        if brace == -1:
            continue
        end = block_extent(body, brace + 1)
        _scan(
            body[brace + 1 : end - 1],
            dict(tainted),
            problems,
            where,
            base_depth=depth,
        )


def _violations() -> list:
    problems = []
    for path in nf_test_files():
        text = strip_comments(path.read_text())
        rel = str(path)
        for m in _TEST_BLOCK.finditer(text):
            brace = text.find("{", m.end())
            if brace == -1:
                continue
            end = block_extent(text, brace + 1)
            body = text[brace + 1 : end - 1]
            offset = text[: brace + 1].count("\n")
            found: list = []
            _scan(body, {}, found, f"{rel}:{offset}")
            problems.extend(found)
    return sorted(set(problems))


def test_no_nf_test_assertion_treats_an_output_string_as_a_file():
    """Every `.nf.test` assertion that reaches for a File member wraps the raw
    output first.

    WATCHED FAILING, twice. Rewriting
    `tests/modules/split_channels.nf.test:50`'s stub assertion from
    `fileList.collect { file(it).name }` to `fileList.collect { it.name }` and
    re-running gave, verbatim:

        tests/modules/split_channels.nf.test:10+14: `it.name` inside a closure
        over a raw nf-test output -- `it` is a path String, not a File.
        Wrap it: file(it).name

    The FIRST attempt at that same edit passed, and that is why the taint walk
    below runs to a fixpoint: `files` came from `get(0).get(1)` but the closure
    ran over `fileList`, bound one line later as `(files instanceof List) ?
    files : [files]`, and a single pass never carried the taint across that
    second binding. A guard that had only ever been run green would have shipped
    with that hole.

    The reported location is `<file>:<line of the test block's brace>+<line
    within the block>` -- nf-test's own failures name a test by id, not by
    line, so pointing at the block start is the useful anchor.
    """
    problems = _violations()
    assert not problems, "\n".join(problems)


def test_the_scan_actually_reaches_real_tagged_blocks():
    """A guard over zero blocks passes. This asserts the walk really does enter
    `real`-tagged test bodies -- the ones CI's blocking `--tag stub` gate never
    runs, and the whole reason this file exists."""
    seen = 0
    for path in nf_test_files():
        text = strip_comments(path.read_text())
        for m in _TEST_BLOCK.finditer(text):
            brace = text.find("{", m.end())
            if brace == -1:
                continue
            end = block_extent(text, brace + 1)
            if 'tag "real"' in text[brace:end]:
                seen += 1
    assert seen >= 20, f"only {seen} real-tagged test blocks found -- the scan is blind"


def test_scan_flags_an_unwrapped_dot_name_in_a_synthetic_real_block():
    """The test above proves `real`-tagged BLOCKS are counted; it says nothing
    about whether `_scan` actually finds anything inside one -- a scan that
    counted blocks but walked none of their bodies would pass it too. This runs
    `_scan` directly against a synthetic block containing exactly one unwrapped
    `.name` access on a tainted output and asserts it is reported, closing that
    gap the way `SIZE_IS_CARDINALITY_BELOW`'s and the taint-fixpoint's own
    watched-failing runs closed theirs."""
    body = """
        then {
            assertAll(
                { assert process.out.nuclei_mask.get(0).get(1).name == 'foo.tif' }
            )
        }
    """
    problems: list = []
    _scan(body, {}, problems, "synthetic:0")
    assert problems, (
        "_scan found nothing in a block with a real, unwrapped .name access"
    )


def test_scan_does_not_flag_the_same_access_once_wrapped_in_file():
    """The NEGATIVE control for the test above: the identical `.name` access,
    wrapped in `file(...)` -- the discharge this whole guard exists to require --
    must report NOTHING. Without this half, a `_scan` that flagged every `.name`
    access unconditionally (wrapped or not) would still pass the positive test
    above; this is what proves the walk is actually looking at the wrap, not
    just pattern-matching `.name`."""
    body = """
        then {
            assertAll(
                { assert file(process.out.nuclei_mask.get(0).get(1)).name == 'foo.tif' }
            )
        }
    """
    problems: list = []
    _scan(body, {}, problems, "synthetic:0")
    assert not problems, (
        f"_scan flagged a file(...)-wrapped .name access as tainted: {problems}"
    )
