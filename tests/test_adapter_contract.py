#!/usr/bin/env python3
"""The registration-adapter seam, asserted instead of commented.

WHY THIS FILE EXISTS. The seam between `registration.nf` and a registration
backend used to be documented by a ~23-line "THE ADAPTER CONTRACT" header
COPIED into both `subworkflows/local/adapters/*_adapter.nf` files — the same
table twice, differing only in the line naming the other file. Nextflow gives no
way to declare a subworkflow's emit shape once, so the comment was the contract
and nothing checked it.

Worse, the comment declared only emit NAMES, and names were never the part that
varied. `transform` is emitted by both adapters under the same name and the same
declared `[patient_id, transform]` shape, with OPPOSITE multiplicity:

  * VALIS  — `REGISTER.out.registrar`, ONE row per PATIENT (a joint alignment
             graph over the whole group; it does not decompose per slide).
  * TILED  — `TILED_SOLVE.out.manifest` re-keyed to `patient_id`, one row per
             MOVING SLIDE.

`seg_qc.nf` therefore had to join the two differently, and picked which join to
use by string-comparing an out-of-band `method` argument against `'tiled'`. A
third backend that filled `transform` per slide, and did not also edit that
`if`, would have been joined by patient alone — `combine(by: 0)` cross-joins, so
N moving slides against N transforms yields N**2 pairs, most of them a slide
scored against another slide's transform. Silently wrong output, not a crash.

So the contract now lives in `lib/AdapterContract.groovy` as DATA — every emit
name AND its cardinality, per backend — and this file is the guard that both
shipped adapters, and any third one, actually satisfy it.

What this file can and cannot see: it is a STATIC check (text of the .nf and
.groovy sources). It proves the declaration exists, is complete, uses the
vocabulary, and agrees with the one statically-visible fact — that a `none`
cardinality is emitted as `Channel.empty()`. It CANNOT observe how many rows a
running adapter emits; that is
tests/subworkflows/local/adapters/adapter_cardinality.nf.test, which runs both
adapters on a 1-reference + 2-moving-slide group and counts.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADAPTER_DIR = ROOT / "subworkflows" / "local" / "adapters"
CONTRACT = ROOT / "lib" / "AdapterContract.groovy"
WARP_BACKENDS = ROOT / "lib" / "WarpBackends.groovy"
SCHEMA = ROOT / "nextflow_schema.json"
SEG_QC = ROOT / "subworkflows" / "local" / "seg_qc.nf"


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def _contract_text() -> str:
    assert CONTRACT.exists(), (
        f"{CONTRACT.relative_to(ROOT)} does not exist. The adapter seam's emit "
        "vocabulary and per-backend cardinality must have ONE owner that is data, "
        "not a comment duplicated into both adapters."
    )
    return CONTRACT.read_text()


def _adapter_files() -> dict[str, Path]:
    """{method: adapter file} — discovered, never hardcoded, so a third adapter
    is covered by every test below the day the file lands."""
    found = {
        p.name[: -len("_adapter.nf")]: p for p in sorted(ADAPTER_DIR.glob("*_adapter.nf"))
    }
    assert found, f"no *_adapter.nf files under {ADAPTER_DIR.relative_to(ROOT)}"
    return found


def _emit_block(text: str) -> list[str]:
    """The emit names a Nextflow workflow declares, in order.

    Scans from the `emit:` label to the end of the workflow body, taking each
    `name = ...` assignment. Comment lines are skipped, which is why the split on
    '=' cannot pick up prose containing an equals sign.
    """
    m = re.search(r"^\s*emit:\s*$", text, re.MULTILINE)
    assert m, "no `emit:` block found"
    names = []
    for raw in text[m.end() :].splitlines():
        line = raw.strip()
        if not line or line.startswith("//") or line.startswith("*") or line == "}":
            continue
        em = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if em:
            names.append(em.group(1))
    return names


def _groovy_string_list(text: str, name: str) -> list[str]:
    """`static final List<String> NAME = ['a', 'b']` -> ['a', 'b']."""
    m = re.search(rf"{name}\s*=\s*\[(.*?)\]", text, re.S)
    assert m, f"{name} not found in {CONTRACT.name}"
    return re.findall(r"'([^']+)'", m.group(1))


def _groovy_map_keys(text: str, name: str) -> list[str]:
    """Top-level keys of a Groovy map literal `NAME = [ k: ..., k2: ... ]`.

    Depth-aware: only keys at bracket depth 1 are returned, so the nested
    per-backend maps inside BACKENDS do not leak into BACKENDS' own key list.
    """
    start = re.search(rf"{name}\s*=\s*\[", text)
    assert start, f"{name} not found in {CONTRACT.name}"
    i = start.end()
    depth = 1
    keys = []
    body_start = i
    while i < len(text) and depth > 0:
        c = text[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
        i += 1
    body = text[body_start : i - 1]
    depth = 0
    for m in re.finditer(r"[\[\]]|^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", body, re.M):
        tok = m.group(0).strip()
        if tok == "[":
            depth += 1
        elif tok == "]":
            depth -= 1
        elif depth == 0 and m.group(1):
            keys.append(m.group(1))
    return keys


def _backend_declaration(text: str, method: str) -> dict[str, str]:
    """The `method: [emit: 'cardinality', ...]` block inside BACKENDS."""
    m = re.search(rf"^\s*{method}\s*:\s*\[", text, re.M)
    assert m, (
        f"lib/AdapterContract.groovy declares no cardinality for backend "
        f"'{method}', but subworkflows/local/adapters/{method}_adapter.nf exists"
    )
    i = m.end()
    depth = 1
    start = i
    while i < len(text) and depth > 0:
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
        i += 1
    body = text[start : i - 1]
    return dict(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([A-Z_]+)\b", body))


# --------------------------------------------------------------------------- #
# The contract itself
# --------------------------------------------------------------------------- #
def test_every_adapter_has_a_cardinality_declaration():
    """A backend with an adapter file but no entry in AdapterContract.BACKENDS is a
    backend whose cardinality nothing knows — exactly the state that let `transform`
    mean two different multiplicities under one name."""
    text = _contract_text()
    declared = set(_groovy_map_keys(text, "BACKENDS"))
    on_disk = set(_adapter_files())
    assert declared == on_disk, (
        "lib/AdapterContract.groovy's BACKENDS table and the adapters on disk "
        f"disagree. Declared: {sorted(declared)}. On disk: {sorted(on_disk)}."
    )


def test_each_backend_declares_every_emit_and_a_valid_cardinality():
    """Names AND cardinality, for every emit, for every backend. A missing emit is
    a consumer that reads `.out.<name>` on a channel that does not exist; a missing
    CARDINALITY is the defect this whole file exists for."""
    text = _contract_text()
    emits = set(_groovy_map_keys(text, "EMITS"))
    assert emits, "AdapterContract.EMITS is empty"
    vocabulary = set(_groovy_string_list(text, "CARDINALITIES"))
    assert vocabulary, "AdapterContract.CARDINALITIES is empty"

    for method in _adapter_files():
        decl = _backend_declaration(text, method)
        # Compared against the constant NAMES (PER_PATIENT, ...), which is what the
        # table is written in; the values those constants hold are checked against
        # CARDINALITIES below.
        assert set(decl) == emits, (
            f"backend '{method}' declares cardinality for {sorted(decl)}, "
            f"but the seam's emits are {sorted(emits)}"
        )
        constants = {
            name: value
            for name, value in re.findall(
                r"static final String ([A-Z_]+)\s*=\s*'([^']+)'", text
            )
        }
        for emit, const in decl.items():
            assert const in constants, (
                f"backend '{method}' declares emit '{emit}' with '{const}', which is "
                f"not one of AdapterContract's cardinality constants {sorted(constants)}"
            )
            assert constants[const] in vocabulary, (
                f"cardinality constant {const} = '{constants[const]}' is not listed "
                "in AdapterContract.CARDINALITIES"
            )


def test_adapter_emit_blocks_match_the_declared_vocabulary():
    """The declaration must describe the adapter that exists, not the one that was
    intended. Checked against each adapter's actual `emit:` block."""
    text = _contract_text()
    emits = set(_groovy_map_keys(text, "EMITS"))
    for method, path in _adapter_files().items():
        actual = set(_emit_block(path.read_text()))
        assert actual == emits, (
            f"{path.relative_to(ROOT)} emits {sorted(actual)}, but the seam's "
            f"declared vocabulary is {sorted(emits)}"
        )


def test_a_none_cardinality_is_emitted_as_the_null_object():
    """`none` means "this method produces no such artifact", and the adapters'
    null-object rule says that is `Channel.empty()` — never a missing emit.

    This is the one cardinality a static reader can verify outright, and it is
    checked in BOTH directions: a declaration of `none` whose emit is a real
    channel is a lie, and an emit wired to `Channel.empty()` while declared
    non-empty is a consumer waiting on rows that never come.
    """
    text = _contract_text()
    none_value = re.search(r"static final String NONE\s*=\s*'([^']+)'", text)
    assert none_value, "AdapterContract.NONE is not declared"

    for method, path in _adapter_files().items():
        decl = _backend_declaration(text, method)
        adapter = path.read_text()
        for emit, const in decl.items():
            wired_empty = bool(
                re.search(rf"^\s*{emit}\s*=\s*Channel\.empty\(\)", adapter, re.M)
            )
            declared_none = const == "NONE"
            assert wired_empty == declared_none, (
                f"{path.relative_to(ROOT)}: emit '{emit}' is "
                f"{'wired to Channel.empty()' if wired_empty else 'wired to a real channel'} "
                f"but declared '{const}' in lib/AdapterContract.groovy"
            )


def test_the_contract_is_not_duplicated_into_the_adapters():
    """The duplicated header is REPLACED by this file, not supplemented by it.

    Two copies of a table drift; the copy nobody edits is the one the next reader
    believes. Each adapter must instead point at the single owner.
    """
    for method, path in _adapter_files().items():
        adapter = path.read_text()
        assert "THE ADAPTER CONTRACT (identical in" not in adapter, (
            f"{path.relative_to(ROOT)} still carries the duplicated contract "
            "header. lib/AdapterContract.groovy owns the emit table now."
        )
        assert "AdapterContract" in adapter, (
            f"{path.relative_to(ROOT)} does not name lib/AdapterContract.groovy — "
            "a reader arriving at the adapter has no route to the contract it must satisfy"
        )


def test_seg_qc_branches_on_declared_cardinality_not_on_a_method_literal():
    """The consumer must ask the contract, not string-match the backend name.

    `if (method == 'tiled')` made the join shape depend on a name travelling out
    of band from `params.registration_method`. The fact that actually selects the
    join is the cardinality of `transform`, so that is what the branch must read.
    """
    text = SEG_QC.read_text()
    code = "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("//")
    )
    # The header comment legitimately names 'tiled' when explaining the two joins;
    # only executable code is scanned.
    assert "'tiled'" not in code and '"tiled"' not in code, (
        "subworkflows/local/seg_qc.nf still branches on the literal backend name. "
        "Branch on AdapterContract's declared cardinality of `transform` instead — "
        "a third backend that fills it per slide must get the per-slide join "
        "without editing this file."
    )
    assert "AdapterContract" in code, (
        "subworkflows/local/seg_qc.nf does not consult lib/AdapterContract.groovy"
    )


def test_the_backend_vocabularies_agree_across_the_repo():
    """One backend name list, three places that must not drift apart.

    A third backend added to the schema enum but not to AdapterContract would fail
    at `AdapterContract.of()`; one added to AdapterContract but not the schema is
    unreachable from the command line. Both are caught here rather than at runtime.
    """
    text = _contract_text()
    contract_methods = set(_groovy_map_keys(text, "BACKENDS"))

    warp = WARP_BACKENDS.read_text()
    warp_block = re.search(r"BACKENDS\s*=\s*\[(.*)\]\.asImmutable\(\)", warp, re.S)
    assert warp_block, "WarpBackends.BACKENDS not found"
    warp_methods = set(
        re.findall(r"^\s{8}([a-z_]+)\s*:\s*\[", warp_block.group(1), re.M)
    )

    schema = json.loads(SCHEMA.read_text())
    enum = set(
        schema["$defs"]["registration_options"]["properties"]["registration_method"][
            "enum"
        ]
    )

    assert contract_methods == warp_methods == enum, (
        "registration backend vocabularies disagree — "
        f"AdapterContract: {sorted(contract_methods)}, "
        f"WarpBackends: {sorted(warp_methods)}, "
        f"nextflow_schema.json enum: {sorted(enum)}"
    )
