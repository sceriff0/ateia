"""A `groupKey()` must never escape the `groupTuple()` it was created for.

WHY THIS EXISTS -- the bug it was written against
-------------------------------------------------
`groupKey(id, size)` is a STREAMING SIZE HINT: it tells `groupTuple()` how many
items a key will receive so the group can close early. It is NOT an identifier.
But `groupTuple()` emits the `nextflow.extension.GroupKey` OBJECT itself as
element 0 of the group, and if the consuming code passes that element on, the
wrapper travels through the rest of the pipeline in place of the plain id.

That is not cosmetic. `GroupKey` delegates `hashCode()` to the id it wraps but
its `equals()` is ASYMMETRIC (verified on Nextflow 25.04.7)::

    groupKey('P001', 2).equals('P001')  ==  true
    'P001'.equals(groupKey('P001', 2))  ==  false
    groupKey('P001', 2).hashCode()      ==  'P001'.hashCode()

Equal hashes put both in the same HashMap bucket, and the one-directional
`equals` then makes the lookup succeed or fail depending only on WHICH key type
was stored first. `combine(by:)`/`join(by:)` store each side in exactly such a
map and cross an arriving item against what the other side has already stored,
so the outcome depends on arrival order:

  * plain-String side arrives first -> stored under the String, the later
    GroupKey lookup HITS   -> the tuple is emitted;
  * GroupKey side arrives first    -> stored under the GroupKey, the later
    String lookup MISSES   -> the tuple is silently dropped.

That is exactly how `subworkflows/local/registration.nf` used to lose a
patient's whole registration QC (`*_seg_qc.json`) in roughly half of all
CPU-contended stub runs, with `WARP_SEG_QC` receiving no input and the run still
exiting 0: the transform's key was the GroupKey from that file's `groupTuple()`,
the GeoJSON's key was the plain `meta.patient_id` String.

THE RULE
--------
A `groupTuple()` fed by a `groupKey()` must hand its result to a `.map {}` as the
VERY NEXT operator in the same chain, and that closure must treat the key
parameter as a grouping artefact rather than an id. Either

  1. discard it -- name the parameter with a leading underscore and never read
     it (`subworkflows/local/adapters/tiled_adapter.nf` does this), or
  2. unwrap it -- every read is `<param>.toString()` (or `<param>[0].toString()`
     for a single-parameter closure, or `<param> as String`, or
     `String.valueOf(<param>)`)
     (`subworkflows/local/registration.nf`,
     `subworkflows/local/quantify_markers.nf` do this).

Anything else re-emits the wrapper and re-opens the bug.

WHY THE CHAIN IS PARSED AND NOT GREPPED
---------------------------------------
The first version of this check found "the next `.groupTuple(` anywhere later in
the file", then "the next `.map {` anywhere after that", and audited whichever
closure that landed on. Review demonstrated two ways that lies:

  * delete the consuming `.map {}` entirely and feed `ch_grouped` straight into
    `REGISTER_PATIENT(...)` -- i.e. reintroduce the exact bug, the GroupKey
    becoming `val(patient_id)` again -- and the check simply audited an
    unrelated `.map {}` further down the file and PASSED;
  * when it did fire, it could name a parameter from a closure 40 lines away, so
    the reported file:line was not the offending site.

So the chain is now walked structurally: from the closure that contains the
`groupKey()`, each following `.name(...)` / `.name {...}` operator is read in
order. The `groupTuple()` must appear in that chain, and the operator
IMMEDIATELY after it must be a `.map {}` -- a `groupTuple()` whose result flows
anywhere else (into a variable used by a process call, into an `emit:`, into any
other operator) fails, because that is precisely how the wrapper escapes.

WHY THE SCAN IS TOKENIZED AND NOT REGEXED
-----------------------------------------
Masking comments alone is not enough either. Review showed that an unmatched
brace inside a STRING LITERAL, sitting inside the consuming closure itself,
truncated the extracted closure body at the stray brace -- before the leaking
read -- so the guard PASSED on genuinely buggy code::

    .groupTuple()
    .map { g, m, f ->
        def label = "unmatched } here in a string"
        def bad = g          // real leak, never reached by the old scan
        [ "x", m, f ]
    }

A guard that silently passes on a real leak is the exact defect class this
branch exists to remove, so `_code_view()` masks comments AND string contents in
one pass: `'...'`, `"..."`, `'''...'''`, triple-double-quoted, slashy `/.../`
and dollar-slashy `$/.../$`, honouring backslash escapes everywhere except
`$/.../$`, where Groovy makes a backslash a literal character. GString
interpolation is deliberately NOT masked -- `${...}` and `$ident` stay verbatim,
so their braces still balance and a read hidden inside an interpolation is still
counted (a GString is not an unwrap: `GString.equals(String)` is false too).

WHAT THIS GUARD GUARANTEES, AND WHAT IT DOES NOT
------------------------------------------------
An earlier revision of this docstring claimed "there is no path on which this
check truncates a body and passes". That was false, and review built the
counterexample: Groovy allows a slashy string after a keyword (`return /re/`),
where the preceding CHARACTER is alphanumeric, so a character-only classifier
read it as division; a stray quote inside the misread body then opened a real
string state that blanked a later leaking read out of the view -- and because
that spurious mask spanned no brackets, the balance assertion never fired. A
guard that overstates its own coverage is the failure this branch exists to
remove, so the honest statement is:

GUARANTEED
  * comments and the string forms below are masked before anything is parsed;
  * the operator chain is walked structurally, so the audited closure is the
    group's real consumer or the check fails (never a lexically nearby one);
  * `_assert_balanced` runs over EVERY scanned `.nf` on every test run, so a
    mis-mask that changes any bracket, brace or parenthesis count RAISES,
    naming file and line;
  * a raw newline inside a `'` or `"` string RAISES -- Groovy forbids it, so
    reaching one means the opening quote was not really a string opener. This
    is the second net under the `/` heuristic, and it is the one that catches a
    spurious mask which hides code WITHOUT touching a delimiter, where
    `_assert_balanced` would see nothing. Watched firing on
    `println /it's <newline> def bad = g <newline> fine/` -- `println` is not
    in `_SLASHY_KEYWORDS`, so the `/` is read as division and the `'` opens a
    spurious string, exactly the shape review built.

NOT GUARANTEED -- known residual limits
  1. The `/` classification is a HEURISTIC. It treats `/` as opening a slashy
     string when the preceding significant token is expression-opening
     punctuation or one of `_SLASHY_KEYWORDS`. A slashy string opened in some
     other expression-start context this list does not name would still be read
     as division. For a leak to then slip through, the misread body would have
     to contain an odd number of quote characters AND the resulting spurious
     mask would have to close before the end of its line AND blank a leaking
     read AND remove no delimiter. Narrow, but not impossible: it is a limit,
     not a guarantee.
  2. When either net fires, the guard REFUSES TO AUDIT and names the delimiter
     or quote it choked on -- which can be nowhere near the leak. Review's
     `return /it's ... fine'` reproducer is now classified as a slashy string
     (correctly: Groovy reads it the same way, and the closure's `}` therefore
     sits INSIDE the string), so the file is genuinely unbalanced and the
     failure reads "registration.nf:51: unclosed '{'" -- the workflow's own
     opening brace, not the `def bad = g` line. Loud and never silent, but a
     refusal to scan, not a diagnosis of the leaking read.
  3. Dollar-slashy `$/.../$` is masked, but its `$$` and `$/` escapes are not
     modelled, so a body containing them can terminate early. Before this
     round the form was not recognised at all and its body was scanned AS CODE
     -- an earlier revision of this list claimed that "raises unterminated
     string literal, which is loud"; it does not, it passed silently.
  4. The group is followed only as far as its own operator chain. A
     `groupTuple()` whose result is assigned to a variable and consumed in a
     later statement is not traced; it fails rule 3 ("ends its chain") instead.
     That is conservative and loud, never silent, but it is a false-positive
     shape a future refactor could legitimately hit.

A hand-rolled Groovy lexer will never be provably complete. Three rounds of
hardening bought the two assertions above; what is left is stated here rather
than chased.

This check is static because the failure is a ~1-in-2 arrival-order race: a
behavioural test passes by luck far too often to be worth anything.
"""

from __future__ import annotations

import re
from pathlib import Path

from _code_view import code_view as _code_view
from _code_view import line_of as _line_of

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("subworkflows", "workflows", "modules", "tests")

_IDENT = r"[A-Za-z_]\w*"
_OPEN = "([{"
_CLOSE = ")]}"


def _match_delim(src: str, i: int) -> int:
    """Index of the delimiter closing the `(`/`[`/`{` at ``i``.

    Type-aware: a mismatched pair raises rather than silently pairing a brace
    with a bracket. Callers only ever pass positions taken from `_code_view()`,
    whose balance assertion has already run over the whole file.
    """
    assert src[i] in _OPEN, f"expected an opening delimiter at {i}, got {src[i]!r}"
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list = []
    for j in range(i, len(src)):
        if src[j] in _OPEN:
            stack.append(src[j])
        elif src[j] in _CLOSE:
            assert stack and stack[-1] == pairs[src[j]], "mismatched delimiters"
            stack.pop()
            if not stack:
                return j
    raise AssertionError("unbalanced delimiters")


def _skip_ws(src: str, i: int) -> int:
    while i < len(src) and src[i] in " \t\r\n":
        i += 1
    return i


def _enclosing_open_brace(src: str, pos: int) -> int | None:
    """Index of the innermost `{` that is still open at ``pos``."""
    depth = 0
    for i in range(pos - 1, -1, -1):
        c = src[i]
        if c == "}":
            depth += 1
        elif c == "{":
            if depth == 0:
                return i
            depth -= 1
    return None


def _chain_ops(src: str, i: int) -> list[dict]:
    """Read the `.name(...)` / `.name {...}` operators that follow position ``i``.

    This is what makes the check structural: a Groovy statement never begins with
    `.`, so everything reachable this way is genuinely the same channel chain,
    and the chain stops exactly where the channel stops being piped onward.
    """
    ops: list[dict] = []
    while True:
        j = _skip_ws(src, i)
        if j >= len(src) or src[j] != ".":
            return ops
        m = re.match(_IDENT, src[j + 1 :])
        if not m:
            return ops
        name = m.group(0)
        k = _skip_ws(src, j + 1 + len(name))
        op = {"name": name, "pos": j, "kind": None, "open": None, "close": None}
        if k < len(src) and src[k] in "({":
            close = _match_delim(src, k)
            op["kind"] = src[k]
            op["open"] = k
            op["close"] = close
            i = close + 1
        else:
            i = j + 1 + len(name)
        ops.append(op)


def _closure_params(src: str, open_brace: int, close_brace: int):
    """(parameter names, body) of a `{ a, b -> ... }` closure, or (None, body).

    The parameter list is only recognised when the text before the first `->`
    holds nothing but identifiers and commas -- otherwise that arrow belongs to
    a nested closure and this one has no declared parameters.
    """
    inner = src[open_brace + 1 : close_brace]
    arrow = inner.find("->")
    if arrow == -1:
        return None, inner
    head = inner[:arrow]
    if not re.fullmatch(r"[\sA-Za-z_,\w]*", head) or any(c in head for c in "(){}[];="):
        return None, inner
    params = [p.strip() for p in head.split(",") if p.strip()]
    return (params or None), inner[arrow + 2 :]


def _group_key_sites() -> list[dict]:
    """Every real `groupKey(...)`, resolved to the chain it belongs to.

    Each record carries the `groupKey()` line, the operators that follow its
    closure, and -- when the chain is well formed -- the consuming `.map {}`'s
    parameters and body. `problem` is set when the chain itself is wrong, in
    which case there is nothing left to audit inside a closure.
    """
    sites: list[dict] = []
    for d in SCAN_DIRS:
        for path in sorted((REPO_ROOT / d).rglob("*.nf")):
            src = _code_view(path)
            for m in re.finditer(r"\bgroupKey\s*\(", src):
                rel = path.relative_to(REPO_ROOT)
                site = {
                    "path": rel,
                    "line": _line_of(src, m.start()),
                    "params": None,
                    "body": None,
                    "map_line": None,
                    "problem": None,
                }
                sites.append(site)
                where = f"{rel}:{site['line']}"

                brace = _enclosing_open_brace(src, m.start())
                if brace is None or _match_delim(src, brace) < m.start():
                    site["problem"] = (
                        f"{where}: this groupKey() is not inside a closure, so the "
                        "chain it belongs to cannot be resolved. Build the key in a "
                        ".map {} that feeds the groupTuple()."
                    )
                    continue

                ops = _chain_ops(src, _match_delim(src, brace) + 1)
                names = [o["name"] for o in ops]
                if "groupTuple" not in names:
                    site["problem"] = (
                        f"{where}: this groupKey() does not feed a groupTuple() in its "
                        f"own chain (operators after it: {names or 'none'}). A groupKey "
                        "outside a groupTuple() is a GroupKey wrapper with no purpose."
                    )
                    continue

                gt = names.index("groupTuple")
                if gt == len(ops) - 1:
                    site["problem"] = (
                        f"{where}: the groupTuple() at "
                        f"{rel}:{_line_of(src, ops[gt]['pos'])} ends its chain, so the "
                        "nextflow.extension.GroupKey wrapper leaves the grouping as the "
                        "group key and reaches whatever consumes this channel -- a "
                        "process input, an emit, another operator. That is the bug this "
                        "guard exists for. Consume it with a .map {} that unwraps or "
                        "discards the key."
                    )
                    continue

                nxt = ops[gt + 1]
                if nxt["name"] != "map" or nxt["kind"] != "{":
                    site["problem"] = (
                        f"{where}: the groupTuple() at "
                        f"{rel}:{_line_of(src, ops[gt]['pos'])} is followed by "
                        f".{nxt['name']}(...) at {rel}:{_line_of(src, nxt['pos'])}, not "
                        "by a .map {} -- so nothing unwraps the GroupKey before it "
                        "travels on. Put the unwrapping .map {} directly after the "
                        "groupTuple()."
                    )
                    continue

                params, body = _closure_params(src, nxt["open"], nxt["close"])
                site["map_line"] = _line_of(src, nxt["pos"])
                site["body"] = body
                if not params:
                    site["problem"] = (
                        f"{where}: the .map {{}} consuming this groupTuple() at "
                        f"{rel}:{site['map_line']} declares no parameters, so the group "
                        "key is reached as `it[0]` and cannot be audited. Name the "
                        "parameters."
                    )
                    continue
                site["params"] = params
    return sites


# Every accepted way of turning the GroupKey back into a plain String. A GString
# (`"${k}"`) is deliberately NOT here: GString.equals(String) is false too, so
# interpolation swaps one asymmetric key for another.
# `.toString()`, `?.toString()` (safe navigation) and `k .toString()` (spaced).
_CALL_TOSTRING = r"\s*\??\s*\.\s*toString\(\)"
_UNWRAP_TAIL = re.compile(
    _CALL_TOSTRING  # k.toString()
    + r"|\s*\[\s*0\s*\]"
    + _CALL_TOSTRING  # k[0].toString()  (single-parameter closure)
    + r"|\s+as\s+String\b"  # k as String
)


def _leaking_reads(key: str, body: str) -> list[re.Match]:
    reads = []
    for mo in re.finditer(rf"\b{re.escape(key)}\b", body):
        tail = body[mo.end() :]
        if _UNWRAP_TAIL.match(tail):
            continue
        if body[: mo.start()].endswith("String.valueOf("):
            continue
        # A map-literal key / named argument (`[patient_id: pid]`) is a label,
        # not a read -- but a Groovy ternary's THEN branch has the identical
        # tail shape (`cond ? group_key : fallback`), and that IS a leak. The
        # two are told apart by what precedes the name: a map key only ever
        # opens an entry, so it follows `[`, `,`, `(` or the start of the body,
        # while a ternary's then-branch follows `?`. Measured: without the head
        # check, `def patient_id = metas.size() > 1 ? group_key : ...` planted
        # in registration.nf passed this guard (2 passed).
        head = body[: mo.start()].rstrip()
        if re.match(r"\s*:(?!:)", tail) and (head == "" or head[-1] in "[,("):
            continue
        if mo.start() and body[mo.start() - 1] == ".":
            continue  # a property of something else that shares the name
        reads.append(mo)
    return reads


def test_leaking_reads_tells_a_map_key_from_a_ternary():
    """The map-literal skip must not swallow a ternary's then-branch.

    `[k: v]` is a label and must be skipped -- `quantify_markers.nf`'s
    `patient_id: pid` entry depends on it, and deleting the skip fails there.
    `cond ? k : v` wears the same `name` + `:` tail and is a real leak. Both
    directions are pinned so neither can regress into the other.
    """
    # map-literal keys and named arguments: skipped
    assert _leaking_reads("k", "[k: 1]") == []
    assert _leaking_reads("k", "def m = [\n    id: x,\n    k: y,\n]") == []
    assert _leaking_reads("k", "foo(k: 1)") == []
    assert _leaking_reads("k", "k: 1") == []
    # ternary then-branch: a genuine leak, on one line and split over three
    assert len(_leaking_reads("k", "def id = n > 1 ? k : other")) == 1
    assert len(_leaking_reads("k", "def id = n > 1\n    ? k\n    : other")) == 1


def test_group_key_sites_are_found():
    """The scan must actually see the groupKey call sites it claims to guard.

    A guard whose glob matches nothing passes silently forever; this pins the
    inventory so deleting or moving one is a deliberate act. Adding a genuinely
    new groupKey() site means updating this list AND satisfying the escape check
    below -- extending the list alone does not make a leak pass.
    """
    sites = _group_key_sites()
    files = sorted({str(s["path"]) for s in sites})
    assert files == [
        "subworkflows/local/adapters/tiled_adapter.nf",
        "subworkflows/local/quantify_markers.nf",
        "subworkflows/local/registration.nf",
    ], f"groupKey() call sites moved; scan found {files}"
    assert len(sites) == 4, f"expected 4 groupKey() sites, found {len(sites)}"


def test_group_key_never_escapes_its_group_tuple():
    """Each grouping closure discards the key, or reads it only unwrapped."""
    problems = []
    for site in _group_key_sites():
        if site["problem"]:
            problems.append(site["problem"])
            continue
        key = site["params"][0]
        where = f"{site['path']}:{site['line']}"
        reads = _leaking_reads(key, site["body"])
        if key.startswith("_"):
            if reads:
                problems.append(
                    f"{where}: the groupTuple() key is named `{key}` at "
                    f"{site['path']}:{site['map_line']}, i.e. declared as discarded, but "
                    f"is read {len(reads)} time(s) in that closure. Rename it and unwrap "
                    "with .toString(), or stop reading it."
                )
        elif reads:
            problems.append(
                f"{where}: the groupTuple() key `{key}` at "
                f"{site['path']}:{site['map_line']} is read {len(reads)} time(s) WITHOUT "
                ".toString(), so the nextflow.extension.GroupKey wrapper escapes the "
                "grouping. GroupKey.equals(String) is true but String.equals(GroupKey) "
                "is false while their hashCodes match, so a downstream "
                "combine(by:)/join(by:) against a plain-String key matches in one "
                f"direction only and silently drops tuples depending on arrival order. "
                f"Unwrap it: `def id = {key}.toString()`."
            )
    assert not problems, "GroupKey escapes its grouping:\n  " + "\n  ".join(problems)
