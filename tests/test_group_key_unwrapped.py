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


def _blank_comments(src: str) -> str:
    """Replace comment bodies with spaces, preserving length and line breaks.

    Keeping the offsets stable means the positions we find afterwards still map
    onto the real file, so failure messages quote real line numbers.
    """
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        two = src[i : i + 2]
        if two == "//":
            j = src.find("\n", i)
            j = n if j == -1 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif two == "/*":
            j = src.find("*/", i + 2)
            j = n if j == -1 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        else:
            i += 1
    return "".join(out)


def _line_of(src: str, pos: int) -> int:
    return src.count("\n", 0, pos) + 1


def _match_delim(src: str, i: int) -> int:
    """Index of the delimiter closing the `(`/`[`/`{` at ``i``."""
    assert src[i] in _OPEN, f"expected an opening delimiter at {i}, got {src[i]!r}"
    depth = 0
    for j in range(i, len(src)):
        if src[j] in _OPEN:
            depth += 1
        elif src[j] in _CLOSE:
            depth -= 1
            if depth == 0:
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
            src = _blank_comments(path.read_text())
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
_UNWRAP_TAIL = re.compile(
    r"\.toString\(\)"  # k.toString()
    r"|\s*\[\s*0\s*\]\s*\.toString\(\)"  # k[0].toString()  (single-param closure)
    r"|\s+as\s+String\b"  # k as String
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
