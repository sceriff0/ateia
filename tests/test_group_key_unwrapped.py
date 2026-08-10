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
one pass: `'...'`, `"..."`, `'''...'''`, triple-double-quoted, and slashy
`/.../`, honouring backslash escapes. GString interpolation is deliberately NOT
masked -- `${...}` and `$ident` stay verbatim, so their braces still balance and
a read hidden inside an interpolation is still counted (a GString is not an
unwrap: `GString.equals(String)` is false too).

And because a tokenizer can always be wrong about something -- Groovy's `/` is
genuinely ambiguous between division and a slashy string -- `_code_view()` ends
by asserting that the masked file's delimiters balance. Anything it
mis-tokenizes therefore RAISES, naming the file and line. There is no path on
which this check truncates a body and passes.

This check is static because the failure is a ~1-in-2 arrival-order race: a
behavioural test passes by luck far too often to be worth anything.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("subworkflows", "workflows", "modules", "tests")

_IDENT = r"[A-Za-z_]\w*"
_OPEN = "([{"
_CLOSE = ")]}"


_SLASHY_PRECEDERS = set("([{,;=~!&|?:+-*%<>^")


def _code_view(path: Path) -> str:
    """The file with comment and string CONTENT blanked, offsets preserved.

    Blanking in place (spaces, newlines kept) means every position found in the
    result still maps onto the real file, so failure messages quote real line
    numbers. GString interpolation -- `${...}` and `$ident.path` -- is kept
    verbatim: its braces must stay balanced with the code around them, and an
    identifier read inside an interpolation is a genuine read.
    """
    src = path.read_text()
    out = list(src)
    n = len(src)
    i = 0
    state = None  # None -> code; else {"q": terminator, "interp": bool}
    interp: list = []  # [[outer string state, brace depth], ...]

    def blank(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    def prev_code_char(pos: int):
        k = pos - 1
        while k >= 0 and out[k] in " \t\r\n":
            k -= 1
        return out[k] if k >= 0 else None

    while i < n:
        if state is None:
            c = src[i]
            if interp:
                # inside `${ ... }`: track depth so its closing brace is found
                if c == "{":
                    interp[-1][1] += 1
                    i += 1
                    continue
                if c == "}":
                    if interp[-1][1] == 0:
                        state = interp.pop()[0]
                        i += 1
                        continue
                    interp[-1][1] -= 1
                    i += 1
                    continue
            if src.startswith("//", i):
                j = src.find("\n", i)
                blank(i, n if j == -1 else j)
                i = n if j == -1 else j
                continue
            if src.startswith("/*", i):
                j = src.find("*/", i + 2)
                assert j != -1, f"{path}: unterminated block comment"
                blank(i, j + 2)
                i = j + 2
                continue
            if src.startswith("'''", i) or src.startswith('"""', i):
                state = {"q": src[i : i + 3], "interp": c == '"'}
                i += 3
                continue
            if c in "'\"":
                state = {"q": c, "interp": c == '"'}
                i += 1
                continue
            if c == "/":
                # Groovy's one genuine ambiguity: slashy string vs division. The
                # preceding significant character settles it, and the balance
                # assertion below catches any case where it does not.
                prev = prev_code_char(i)
                if prev is None or prev in _SLASHY_PRECEDERS:
                    state = {"q": "/", "interp": True}
                    i += 1
                    continue
            i += 1
            continue

        # --- inside a string literal ---
        q = state["q"]
        c = src[i]
        if c == "\\":
            blank(i, i + 2)
            i += 2
            continue
        if src.startswith(q, i):
            blank(i, i + len(q))
            state = None
            i += len(q)
            continue
        if state["interp"] and src.startswith("${", i):
            interp.append([state, 0])  # `${` kept verbatim: braces must balance
            state = None
            i += 2
            continue
        if (
            state["interp"]
            and c == "$"
            and i + 1 < n
            and (src[i + 1].isalpha() or src[i + 1] == "_")
        ):
            j = i + 1
            while j < n and (src[j].isalnum() or src[j] in "_."):
                j += 1
            i = j  # `$ident.path` kept verbatim: it is a real read
            continue
        blank(i, i + 1)
        i += 1

    assert state is None and not interp, (
        f"{path}: unterminated string literal -- refusing to audit this file "
        "rather than scan a truncated view of it."
    )
    code = "".join(out)
    _assert_balanced(path, code)
    return code


def _assert_balanced(path: Path, code: str) -> None:
    """The masked file's delimiters must balance, or the mask is wrong.

    This is the loudness net under `_code_view`. If any string form was
    mis-tokenized, the delimiters it wrongly hid or exposed will almost certainly
    unbalance here, and the guard RAISES instead of quietly auditing a truncated
    closure body -- which is exactly how the previous revision could pass on a
    real leak.
    """
    pairs = {")": "(", "]": "[", "}": "{"}
    stack: list = []
    for i, c in enumerate(code):
        if c in "([{":
            stack.append((c, i))
        elif c in ")]}":
            assert stack and stack[-1][0] == pairs[c], (
                f"{path}:{_line_of(code, i)}: delimiters do not balance after "
                "comment/string masking, so the scan cannot be trusted. Refusing "
                "to audit rather than risk a truncated closure body."
            )
            stack.pop()
    assert not stack, (
        f"{path}:{_line_of(code, stack[0][1])}: unclosed {stack[0][0]!r} after "
        "comment/string masking, so the scan cannot be trusted."
    )


def _line_of(src: str, pos: int) -> int:
    return src.count("\n", 0, pos) + 1


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
        if re.match(r"\s*:(?!:)", tail):
            continue  # a map-literal key / named argument, not a read
        if mo.start() and body[mo.start() - 1] == ".":
            continue  # a property of something else that shares the name
        reads.append(mo)
    return reads


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
