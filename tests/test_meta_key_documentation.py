#!/usr/bin/env python3
"""Guard: every meta-map key the pipeline ATTACHES is documented in a TRACKED file.

Why this guard, and why here. The meta contract was described in exactly one place --
the repo's `CLAUDE.md`, which `.gitignore:64` excludes, so it is not in any checkout and
not in any worktree. That description listed eight keys; the code attaches ten. Three of
them (`qc_slide`, `is_passthrough`, `tiles_count`) were undocumented anywhere a reader
who clones this repo can see. Prose alone would rot the same way, so the canonical list
lives in `docs/meta_contract.md` and this test ties it to the code in BOTH directions:

  * every key added by a `<...>meta + [key: ...]` map-addition must appear in the doc
    (an undocumented new key fails);
  * every key the doc lists must actually be attached somewhere or read somewhere
    (a key deleted from the code but left in the doc fails).

HOW THE SCAN IS SCOPED, and why it is not a generic `+ [` sweep. Groovy map-addition is
used for plenty of things that are not meta maps -- `opts + [key: ...]` in
lib/PatientGroup.groovy, `counts + [stop: ...]` in workflows/mirage.nf. A sweep of every
`+ [` would have reported `key` and `stop` as undocumented meta keys, i.e. it would have
failed for reasons that have nothing to do with the contract, and the natural "fix" is to
allowlist them -- at which point the allowlist, not the code, decides what a meta key is.
Instead the LEFT-HAND SIDE of the addition must name a meta: an identifier ending in
`meta` (`meta`, `channel_meta`, `prior.ref_meta`, `prior.post_meta`) or a grouped-item
subscript (`ref[0]`, `ref_item[0]`). That is exactly the set of shapes the codebase uses
and it excludes both false positives structurally rather than by name.

KEY EXTRACTION IS BRACKET-DEPTH AWARE. `meta + [qc_slide: img.name.replaceAll(/re/, '')]`
contains a comma INSIDE a call, so splitting the body on commas produced fragments that
did not parse as `key:` and the whole addition was silently dropped -- the scanner found
nine of the ten keys and reported success. Keys are now taken only at nesting depth 0 of
the bracket body, which is where a map key can be.

THE BASE MAP IS ANCHORED SEPARATELY. `CsvUtils.parseMetadata` builds the initial meta as
a plain literal (no `+`), so no addition-scan can see it. Rather than widen the scan (and
with it the false-positive surface), test_base_meta_map_is_exactly_the_documented_three
asserts that literal's key set directly: if a fourth key is ever added at the base, that
test fails and the doc must grow.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "meta_contract.md"
SCAN_DIRS = ("subworkflows", "workflows", "modules", "lib")
SCAN_SUFFIXES = (".nf", ".groovy")

# The three keys CsvUtils.parseMetadata puts on every meta before anything is added.
BASE_KEYS = {"patient_id", "is_reference", "channels"}

# `<lhs> + [` where <lhs> names a meta map. Two shapes, both real:
#   meta / channel_meta / prior.ref_meta / prior.post_meta   -- identifier ending in "meta"
#   ref[0] / ref_item[0]                                     -- a [meta, file] item's meta
META_ADDITION_RE = re.compile(r"(?:(?:\w+\.)?\w*meta|\w+\[0\])\s*\+\s*\[")

COMMENT_LINE_RE = re.compile(r"//.*$", re.M)
COMMENT_BLOCK_RE = re.compile(r"/\*.*?\*/", re.S)
KEY_AT_DEPTH0_RE = re.compile(r"^\s*(\w+)\s*:")


def _strip_comments(text: str) -> str:
    """Remove Groovy block and line comments.

    A comment mentioning a key name must not count as attaching it, and a commented-out
    map addition must not either -- the same hollow-guard shape that let a bare mention
    of `ProcessEnvelope.` satisfy tests/test_versions_envelope.py.
    """
    text = COMMENT_BLOCK_RE.sub("", text)
    # Only strip `//` that is not part of a URI scheme (`https://`).
    return "\n".join(COMMENT_LINE_RE.sub("", re.sub(r"(?<=:)//", "\x00\x00", line)).replace("\x00\x00", "//") for line in text.split("\n"))


def _bracket_body(text: str, open_idx: int) -> str | None:
    """The substring inside the `[` at `open_idx`, respecting nested (), [] and {}."""
    depth = 0
    for i in range(open_idx, len(text)):
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return text[open_idx + 1 : i]
    return None


def _keys_at_top_level(body: str) -> list[str]:
    """Map keys declared at nesting depth 0 of a bracket body."""
    keys: list[str] = []
    depth = 0
    segment_start = 0
    segments: list[str] = []
    for i, c in enumerate(body):
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            segments.append(body[segment_start:i])
            segment_start = i + 1
    segments.append(body[segment_start:])
    for seg in segments:
        m = KEY_AT_DEPTH0_RE.match(seg)
        if m:
            keys.append(m.group(1))
    return keys


def _scan_files() -> list[Path]:
    files: list[Path] = []
    for d in SCAN_DIRS:
        for p in sorted((ROOT / d).rglob("*")):
            if p.suffix in SCAN_SUFFIXES:
                files.append(p)
    return files


def attached_meta_keys() -> dict[str, set[str]]:
    """key -> set of files that attach it via a meta map-addition."""
    found: dict[str, set[str]] = {}
    for p in _scan_files():
        text = _strip_comments(p.read_text(encoding="utf-8"))
        for m in META_ADDITION_RE.finditer(text):
            body = _bracket_body(text, m.end() - 1)
            if body is None:
                continue
            for k in _keys_at_top_level(body):
                found.setdefault(k, set()).add(str(p.relative_to(ROOT)))
    return found


def documented_keys() -> set[str]:
    """Keys the doc declares, read from its `| \\`key\\` |` table rows."""
    if not DOC.exists():
        return set()
    return set(re.findall(r"^\|\s*`(\w+)`\s*\|", DOC.read_text(encoding="utf-8"), re.M))


def test_meta_contract_doc_exists_and_is_tracked():
    assert DOC.exists(), (
        f"{DOC.relative_to(ROOT)} is missing. The meta contract must live in a TRACKED "
        "file -- CLAUDE.md is gitignored (.gitignore:64) and is absent from every "
        "checkout, so a contract described only there is documented nowhere."
    )


def test_every_attached_meta_key_is_documented():
    attached = attached_meta_keys()
    documented = documented_keys()
    undocumented = {k: sorted(v) for k, v in attached.items() if k not in documented}
    assert not undocumented, (
        f"meta keys attached in code but absent from {DOC.relative_to(ROOT)}: "
        f"{undocumented}. Add a row for each (what it means, who sets it, who reads it)."
    )


def test_every_documented_meta_key_is_real():
    """The inverse direction: the doc must not list a key nothing uses.

    Without this, deleting a key from the code leaves a row in the table describing a
    contract that no longer exists -- and the forward check above would stay green
    forever, because it only ever looks for keys the code has.
    """
    attached = set(attached_meta_keys()) | BASE_KEYS
    text_blob = "\n".join(
        _strip_comments(p.read_text(encoding="utf-8")) for p in _scan_files()
    )
    phantom = []
    for k in sorted(documented_keys()):
        if k in attached:
            continue
        # Not attached anywhere -- it must at least be READ (`meta.<key>` / `meta['<key>']`).
        if re.search(rf"\bmeta\s*\.\s*{re.escape(k)}\b", text_blob) or re.search(
            rf"\bmeta\s*\[\s*['\"]{re.escape(k)}['\"]\s*\]", text_blob
        ):
            continue
        phantom.append(k)
    assert not phantom, (
        f"{DOC.relative_to(ROOT)} documents meta key(s) that are neither attached nor "
        f"read anywhere under {SCAN_DIRS}: {phantom}. Delete the row, or the doc "
        "describes a contract the code does not have."
    )


def test_base_meta_map_is_exactly_the_documented_three():
    """CsvUtils.parseMetadata's literal is invisible to the addition scan -- anchor it."""
    src = (ROOT / "lib" / "CsvUtils.groovy").read_text(encoding="utf-8")
    m = re.search(r"static Map parseMetadata\(.*?def meta = \[(.*?)\]", src, re.S)
    assert m, "could not locate CsvUtils.parseMetadata's base meta literal"
    keys = set(_keys_at_top_level(m.group(1)))
    assert keys == BASE_KEYS, (
        f"CsvUtils.parseMetadata now builds {sorted(keys)}, not {sorted(BASE_KEYS)}. "
        f"Update BASE_KEYS here AND the table in {DOC.relative_to(ROOT)}."
    )
    assert BASE_KEYS <= documented_keys(), (
        f"the base meta keys {sorted(BASE_KEYS - documented_keys())} are undocumented"
    )


def test_scanner_finds_the_awkward_shapes_it_was_written_for():
    """Proof the scanner is not silently finding nothing.

    Each of these was a real miss at some point: `qc_slide` is attached inside a
    map-addition whose value contains commas nested in method calls (the naive
    comma-split dropped the whole addition); `tiles_count` and `is_passthrough` are the
    keys the pre-existing documentation omitted; `is_reference` is attached via a
    `prior.ref_meta + [...]` left-hand side, which a bare `meta + [` literal would miss.
    """
    attached = attached_meta_keys()
    for key, expected_file in (
        ("qc_slide", "subworkflows/local/seg_qc.nf"),
        ("tiles_count", "subworkflows/local/adapters/tiled_adapter.nf"),
        ("is_passthrough", "subworkflows/local/register_patient.nf"),
        ("is_reference", "subworkflows/local/add_cycle.nf"),
    ):
        assert key in attached, f"scanner failed to find `{key}` anywhere"
        assert expected_file in attached[key], (
            f"scanner found `{key}` but not in {expected_file}: {sorted(attached[key])}"
        )


def test_a_comment_does_not_count_as_attaching_a_key(tmp_path: Path):
    """A commented-out map addition must not satisfy the forward check.

    Plants both comment forms around a fabricated key and asserts the extractor sees
    nothing -- the same defeat-by-comment shape this repo has already been bitten by.
    """
    planted = (
        "// meta + [planted_line_comment: 1]\n"
        "/* meta + [planted_block_comment: 2] */\n"
        "def x = meta + [planted_real_key: 3]\n"
    )
    stripped = _strip_comments(planted)
    assert "planted_line_comment" not in stripped
    assert "planted_block_comment" not in stripped
    assert "planted_real_key" in stripped
    m = META_ADDITION_RE.search(stripped)
    assert m, "the plant's real addition must still be found after comment stripping"
    assert _keys_at_top_level(_bracket_body(stripped, m.end() - 1)) == ["planted_real_key"]
