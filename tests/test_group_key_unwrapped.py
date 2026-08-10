"""A `groupKey()` must never escape the `groupTuple()` it was created for.

WHY THIS EXISTS -- the bug it was written against
-------------------------------------------------
`groupKey(id, size)` is a STREAMING SIZE HINT: it tells `groupTuple()` how many
items a key will receive so the group can close early. It is NOT an identifier.
But `groupTuple()` emits the `nextflow.extension.GroupKey` OBJECT itself as
element 0 of the group, and if the consuming closure passes that element on, the
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
Every `.map {}` that consumes a `groupTuple()` whose key came from `groupKey()`
must treat the key parameter as a *grouping artefact*, not as an id. Either

  1. discard it -- name the parameter with a leading underscore and never read
     it (`subworkflows/local/adapters/tiled_adapter.nf` does this), or
  2. unwrap it -- every read of the parameter must be `<param>.toString()`
     (`subworkflows/local/registration.nf`,
     `subworkflows/local/quantify_markers.nf` do this).

Anything else re-emits the wrapper and re-opens the bug. This check is static
because the failure is a ~1-in-2 arrival-order race: a behavioural test passes
by luck far too often to be worth anything.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("subworkflows", "workflows", "modules")


def _blank_comments(src: str) -> str:
    """Replace comment bodies with spaces, preserving length and line breaks.

    Keeping the offsets stable means the byte positions we find afterwards still
    map onto the real file, so failure messages can quote real line numbers.
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


def _matching_brace(src: str, open_pos: int) -> int:
    """Index just past the `}` that closes the `{` at ``open_pos``."""
    depth = 0
    for i in range(open_pos, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    raise AssertionError("unbalanced braces")


def _nf_sources() -> list[Path]:
    files: list[Path] = []
    for d in SCAN_DIRS:
        files.extend(sorted((REPO_ROOT / d).rglob("*.nf")))
    return files


def _group_key_sites() -> list[tuple[Path, int, str, str]]:
    """Every real `groupKey(...)` -> `groupTuple()` -> `.map {}` chain.

    Returns (path, line of the groupKey, key parameter name, closure body).
    """
    sites: list[tuple[Path, int, str, str]] = []
    for path in _nf_sources():
        raw = path.read_text()
        src = _blank_comments(raw)
        for m in re.finditer(r"\bgroupKey\s*\(", src):
            line = _line_of(src, m.start())
            where = f"{path.relative_to(REPO_ROOT)}:{line}"

            gt = re.search(r"\.groupTuple\s*\(", src[m.end() :])
            assert gt, f"{where}: groupKey() with no following groupTuple()"
            after_gt = m.end() + gt.end()

            mp = re.search(r"\.map\s*\{", src[after_gt:])
            assert mp, (
                f"{where}: the groupTuple() fed by this groupKey() is not consumed "
                "by a .map {} closure, so this guard cannot see whether the "
                "GroupKey wrapper is unwrapped. Consume it with .map {}."
            )
            brace = after_gt + mp.end() - 1
            close = _matching_brace(src, brace)
            closure = src[brace + 1 : close]

            arrow = closure.find("->")
            assert arrow != -1, (
                f"{where}: the .map {{}} consuming this groupTuple() has no explicit "
                "parameter list, so the group key is reached as `it[0]` and cannot "
                "be checked. Name the parameters."
            )
            params = [p.strip() for p in closure[:arrow].split(",")]
            sites.append((path, line, params[0], closure[arrow + 2 :]))
    return sites


def test_group_key_sites_are_found():
    """The scan must actually see the groupKey call sites it claims to guard.

    A guard whose glob matches nothing passes silently forever; this pins the
    number of real (non-comment) sites so deleting one is a deliberate act.
    """
    sites = _group_key_sites()
    files = sorted({str(p.relative_to(REPO_ROOT)) for p, _, _, _ in sites})
    assert files == [
        "subworkflows/local/adapters/tiled_adapter.nf",
        "subworkflows/local/quantify_markers.nf",
        "subworkflows/local/registration.nf",
    ], f"groupKey() call sites moved; scan found {files}"
    assert len(sites) == 4, f"expected 4 groupKey() sites, found {len(sites)}"


def test_group_key_never_escapes_its_group_tuple():
    """Each grouping closure discards the key, or reads it only via .toString()."""
    problems = []
    for path, line, key, body in _group_key_sites():
        where = f"{path.relative_to(REPO_ROOT)}:{line}"
        reads = []
        for mo in re.finditer(rf"\b{re.escape(key)}\b", body):
            tail = body[mo.end() :]
            if tail.startswith(".toString()"):
                continue  # unwrapped -- the whole point
            if re.match(r"\s*:(?!:)", tail):
                continue  # a map-literal key / named argument, not a read
            if mo.start() and body[mo.start() - 1] == ".":
                continue  # a property of something else that shares the name
            reads.append(mo)
        if key.startswith("_"):
            if reads:
                problems.append(
                    f"{where}: grouping key `{key}` is named as discarded "
                    f"(leading underscore) but is read {len(reads)} time(s) in the "
                    "closure. Rename it and unwrap with .toString(), or stop reading it."
                )
        elif reads:
            problems.append(
                f"{where}: the groupTuple() key `{key}` is read {len(reads)} time(s) "
                "WITHOUT .toString(), so the nextflow.extension.GroupKey wrapper "
                "escapes the grouping. GroupKey.equals(String) is true but "
                "String.equals(GroupKey) is false while their hashCodes match, so a "
                "downstream combine(by:)/join(by:) against a plain-String key matches "
                "in one direction only and silently drops tuples depending on arrival "
                "order. Unwrap it: `def id = {key}.toString()`."
            )
    assert not problems, "GroupKey escapes its grouping:\n  " + "\n  ".join(problems)
