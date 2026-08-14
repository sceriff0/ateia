"""The STARE tile-plan CSV schema has ONE owner, in each of the two languages that touch it.

The tile plan is the contract between TILED_COARSE (which writes it), the Nextflow fan-out
(which splits it row by row) and TILED_REG_TILE (which reads a row back as CLI flags). Its
column list used to be written out by hand in three independent places — ``bin/tiled_coarse.py``
(the real header), ``modules/local/tiled_coarse.nf``'s stub (a printf'd literal) and
``modules/local/tiled_reg_tile.nf`` (field-by-field ``${row.rx0}`` interpolation). Nothing tied
them together, so a renamed or reordered column would surface as a stub that no longer matches
the real artifact, or as ``--rx0 null`` in a rendered command.

Two owners are unavoidable (the writer is Python, the stub and the consumer are Groovy), so this
file is the seam that makes them one: ``bin/utils/tile_grid.py:TILE_PLAN_COLUMNS`` and
``lib/TilePlan.groovy:COLUMNS`` must agree, nobody may restate the list, and every column the
consumer renders must be a flag ``bin/tiled_reg_tile.py`` actually accepts.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin" / "utils"))

from tile_grid import TILE_PLAN_COLUMNS, tile_grid, write_tile_plan  # noqa: E402

GROOVY = ROOT / "lib" / "TilePlan.groovy"
COARSE_PY = ROOT / "bin" / "tiled_coarse.py"
REG_TILE_PY = ROOT / "bin" / "tiled_reg_tile.py"
COARSE_NF = ROOT / "modules" / "local" / "tiled_coarse.nf"
REG_TILE_NF = ROOT / "modules" / "local" / "tiled_reg_tile.nf"
LIB_PROBE = ROOT / "tests" / "lib_probe.nf"

# The only two files allowed to spell the header out: this guard, and the lib/ probe that
# ASSERTS TilePlan.header()'s value. Both compare against the literal rather than consume
# it — an exemption for a use site would defeat the whole check, so
# test_the_header_exemptions_are_assertions_not_uses keeps these two honest.
_NAMES_THE_HEADER_ON_PURPOSE = {Path(__file__).resolve(), LIB_PROBE.resolve()}


def _groovy_list(name: str) -> list[str]:
    """Pull a `static final List<String> NAME = ['a', 'b', ...]` out of lib/TilePlan.groovy."""
    text = GROOVY.read_text()
    m = re.search(rf"{name}\s*=\s*\[(.*?)\]", text, re.S)
    assert m, f"lib/TilePlan.groovy declares no {name} list"
    return re.findall(r"'([^']+)'", m.group(1))


def test_the_two_language_owners_declare_the_same_columns_in_the_same_order():
    assert _groovy_list("COLUMNS") == list(TILE_PLAN_COLUMNS), (
        "lib/TilePlan.groovy:COLUMNS and bin/utils/tile_grid.py:TILE_PLAN_COLUMNS have "
        "drifted; the stub would no longer match the artifact TILED_COARSE really writes"
    )


def test_the_consumed_columns_are_a_subset_of_the_plan():
    consumed = _groovy_list("REG_TILE_COLUMNS")
    assert consumed, "no REG_TILE_COLUMNS declared"
    assert set(consumed) <= set(TILE_PLAN_COLUMNS), (
        f"TILED_REG_TILE renders columns the tile plan does not contain: "
        f"{sorted(set(consumed) - set(TILE_PLAN_COLUMNS))}"
    )


def test_every_consumed_column_is_a_flag_the_python_consumer_accepts():
    """A column name IS the flag name; a rename that misses one side renders `--foo null`."""
    declared = re.findall(r'add_argument\(\s*"--([a-z0-9_]+)"', REG_TILE_PY.read_text())
    for col in _groovy_list("REG_TILE_COLUMNS"):
        assert col in declared, (
            f"lib/TilePlan.groovy renders --{col} but bin/tiled_reg_tile.py declares no "
            f"such argument (declared: {sorted(set(declared))})"
        )


def test_nobody_restates_the_column_list():
    """The literal header may appear nowhere at all — both owners build it from a list.

    Scope is deliberately wide (bin/, lib/, modules/, subworkflows/, workflows/, tests/):
    the restatement this replaced was in three places, and a fourth was hiding in
    ``tests/testdata/generate_complete_testdata.py``'s tile-plan fixtures, where a drift
    would have made the fan-in gather's own fixtures the wrong shape.
    """
    header = ",".join(TILE_PLAN_COLUMNS)
    scanned = []
    offenders = []
    for pattern in (
        "bin/**/*.py",
        "lib/*.groovy",
        "modules/**/*.nf",
        "subworkflows/**/*.nf",
        "workflows/*.nf",
        "tests/**/*.py",
        "tests/**/*.nf",
    ):
        for path in sorted(ROOT.glob(pattern)):
            if path.resolve() in _NAMES_THE_HEADER_ON_PURPOSE:
                continue
            scanned.append(path)
            if header in path.read_text():
                offenders.append(str(path.relative_to(ROOT)))
    assert len(scanned) > 50, (
        f"the scan only saw {len(scanned)} files -- globs are stale"
    )
    assert not offenders, (
        f"{len(offenders)} file(s) hand-write the tile-plan header instead of building it "
        f"from the schema owner (TilePlan.header() / TILE_PLAN_COLUMNS): {offenders}"
    )
    # the Python writer must not spell the columns out at its call site either
    coarse = COARSE_PY.read_text()
    assert '"ix", "iy"' not in coarse and "'ix', 'iy'" not in coarse, (
        "bin/tiled_coarse.py hand-writes the tile-plan columns; use "
        "tile_grid.write_tile_plan / TILE_PLAN_COLUMNS"
    )
    # ...and the consumer must not interpolate the row field by field
    reg_tile = REG_TILE_NF.read_text()
    assert "${row.rx0}" not in reg_tile, (
        "modules/local/tiled_reg_tile.nf hand-renders the per-tile flags; use "
        "TilePlan.regTileArgs(row)"
    )
    for nf in (COARSE_NF, REG_TILE_NF):
        assert "TilePlan." in nf.read_text(), (
            f"{nf.relative_to(ROOT)} does not go through the TilePlan owner"
        )


def test_write_tile_plan_emits_exactly_the_declared_schema(tmp_path):
    """The header and the field order of a written row both come from the one list."""
    tiles = tile_grid(48, 32, 16, 4)
    out = tmp_path / "tiles.csv"
    write_tile_plan(out, tiles)

    lines = out.read_text().splitlines()
    assert lines[0] == ",".join(TILE_PLAN_COLUMNS)
    assert len(lines) == len(tiles) + 1

    first = dict(zip(TILE_PLAN_COLUMNS, lines[1].split(",")))
    t = tiles[0]
    assert (int(first["ix"]), int(first["iy"])) == (t.ix, t.iy)
    assert (float(first["cx"]), float(first["cy"])) == (t.cx, t.cy)
    assert tuple(int(first[c]) for c in ("x0", "y0", "x1", "y1")) == t.core
    assert tuple(int(first[c]) for c in ("rx0", "ry0", "rx1", "ry1")) == t.read


def test_the_stub_row_covers_every_column():
    """A stub that omits a column publishes a CSV the real run would never produce."""
    text = GROOVY.read_text()
    m = re.search(r"STUB_TILE\s*=\s*\[(.*?)\]", text, re.S)
    assert m, "lib/TilePlan.groovy declares no STUB_TILE"
    keys = re.findall(r"(\w+)\s*:", m.group(1))
    assert keys == list(TILE_PLAN_COLUMNS)


@pytest.mark.parametrize("path", [COARSE_PY, REG_TILE_PY])
def test_the_python_scripts_still_exist_where_this_guard_expects_them(path):
    """A moved script would make the greps above vacuously pass."""
    assert path.exists(), (
        f"{os.fspath(path)} is missing; this guard is checking nothing"
    )


def test_the_header_exemptions_are_assertions_not_uses():
    """Both exempted files must COMPARE against the literal header, never emit it."""
    header = ",".join(TILE_PLAN_COLUMNS)
    probe = LIB_PROBE.read_text()
    assert f"assert TilePlan.header() == '{header}'" in probe, (
        "tests/lib_probe.nf is exempted from the no-restatement scan because it pins "
        "TilePlan.header()'s value; that assertion is gone, so the exemption is now a hole"
    )
    assert 'header = ",".join(TILE_PLAN_COLUMNS)' in Path(__file__).read_text()
