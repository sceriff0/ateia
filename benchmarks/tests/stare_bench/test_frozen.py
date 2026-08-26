"""Pin that the ruler is declared frozen and the committed sweep is on record.

FROZEN.md is the freeze record for this benchmark: what is fixed (the
generator, metrics and committed sweep) versus what is still outstanding
(mid- and gigapixel-rung measurements). These tests only check that the
document exists and states the version and sweep it claims to freeze --
they cannot check that the prose is honest, which is why the document
itself has to be read by a human before it is trusted.
"""

from pathlib import Path

from benchmarks.stare_bench import __version__
from benchmarks.stare_bench.plan import BLANK_FRACTIONS

FROZEN = Path("benchmarks/stare_bench/FROZEN.md")


def test_the_ruler_is_declared_frozen():
    assert FROZEN.exists()
    text = FROZEN.read_text()
    assert __version__ in text


def test_frozen_records_the_committed_sweep():
    text = FROZEN.read_text()
    for f in BLANK_FRACTIONS:
        assert f"{f:.2f}" in text


def test_version_is_one_point_zero():
    assert __version__.startswith("1.")
