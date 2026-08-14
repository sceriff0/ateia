"""The statistic vocabulary lives in three languages. This pins them together.

`bin/utils/measurements.py` owns it. `lib/ParamUtils.groovy` mirrors it because
Nextflow code cannot import Python, and `nextflow_schema.json` mirrors it again
because nf-schema needs a literal enum. Three copies of one list is exactly the
shape that produced the `VALID_STATS` defect in
`bin/utils/phenotyping/panel_schema.py` -- a hardcoded {Mean, Median, Sum} that
silently rejected a column the pipeline really emitted.

Each copy is COMPOSED from base x normalisation rather than written out, so this
test mostly guards the composition inputs. That is the point: a new base statistic
should be a one-line change in each of three files, and this test fails loudly if
it is a one-line change in only two.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin" / "utils"))

from measurements import (  # noqa: E402
    BASE_STATISTICS,
    NORMALIZATIONS,
    REDSEA_STATISTIC,
    STATISTICS,
    split_statistic,
    zscore,
)

PARAM_UTILS = ROOT / "lib" / "ParamUtils.groovy"
SCHEMA = ROOT / "nextflow_schema.json"


def _groovy_list(name: str) -> list:
    """Extract a `static final List<String> NAME = ['a', 'b'].asImmutable()` literal."""
    text = PARAM_UTILS.read_text()
    m = re.search(rf"static final List<String> {name} = \[(.*?)\]\.asImmutable\(\)", text, re.S)
    assert m, f"ParamUtils.{name} not found — did it move or change shape?"
    return re.findall(r"'([^']*)'", m.group(1))


def _schema_enum() -> list:
    d = json.loads(SCHEMA.read_text())
    defs = d.get("$defs") or d.get("definitions")
    return defs["quantification_options"]["properties"]["quantify_statistics"]["items"]["enum"]


# ── the three copies agree ────────────────────────────────────────────────────
def test_groovy_composition_inputs_match_python():
    assert _groovy_list("BASE_STATISTICS") == list(BASE_STATISTICS)
    assert _groovy_list("NORMALIZATIONS") == list(NORMALIZATIONS)


def test_schema_enum_is_the_full_vocabulary():
    """Order too, not just membership — the enum is what users read in the docs."""
    assert _schema_enum() == list(STATISTICS)


def test_the_composition_is_actually_a_cross_product():
    expected = [b + n for b in BASE_STATISTICS for n in NORMALIZATIONS]
    assert list(STATISTICS) == expected
    assert len(STATISTICS) == len(BASE_STATISTICS) * len(NORMALIZATIONS)


# ── the names stay parseable ──────────────────────────────────────────────────
def test_no_statistic_name_is_a_suffix_of_another():
    """Suffix-matching parsers (Mirage's parse_measurement_key, FlowPath's
    MeasurementKeys.parse) resolve a key by testing ": <Comp>: <Stat>" endings.

    If one statistic name ended with another, the shorter could match a key that
    belongs to the longer, silently misattributing the column. "Median Z" ending
    with "Z" is fine — what must not happen is one FULL name ending with another
    full name.
    """
    for a in STATISTICS:
        for b in STATISTICS:
            if a != b:
                assert not a.endswith(b), f"{a!r} ends with {b!r} — parsers will collide"


def test_split_statistic_round_trips_every_name():
    for name in STATISTICS:
        base, norm = split_statistic(name)
        assert base in BASE_STATISTICS
        assert norm in NORMALIZATIONS
        assert base + norm == name


def test_split_prefers_the_longest_normalisation():
    """' RobustZ' must not be mis-split by a greedy shorter match."""
    assert split_statistic("Mean RobustZ") == ("Mean", " RobustZ")
    assert split_statistic("Mean Z") == ("Mean", " Z")


def test_names_contain_a_space_so_splitters_must_use_commas():
    """Pins WHY every parameter splitter is comma-only.

    An earlier whitespace-splitting resolver turned "Mean Z" into "Mean" and an
    unknown statistic "Z". If a future name loses its space this test stops being
    meaningful, so it asserts the space is really there.
    """
    assert any(" " in s for s in STATISTICS)
    assert "Mean Z" in STATISTICS


# ── the z-score itself ────────────────────────────────────────────────────────
def test_zscore_standardises():
    import numpy as np

    v = np.array([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0])
    z = zscore(v)
    assert z.mean() == pytest.approx(0.0, abs=1e-12)
    assert z.std() == pytest.approx(1.0, abs=1e-12)


def test_robust_zscore_is_not_dragged_by_an_outlier():
    """The reason RobustZ exists. One very bright cell inflates SD and squashes
    everyone else toward zero; median/MAD is unmoved."""
    import numpy as np

    base = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    spiked = np.append(base, 1000.0)
    classic_shift = abs(zscore(spiked)[:5]).max() / abs(zscore(base)).max()
    robust_shift = abs(zscore(spiked, robust=True)[:5]).max() / abs(
        zscore(base, robust=True)
    ).max()
    # Measured: classic retains 0.32 of the original spread, robust 0.83 — robust
    # is ~2.6x less compressed. Asserted as a COMPARISON rather than an absolute
    # threshold, because robust is not literally unmoved: adding a sixth point
    # shifts the median from 3.0 to 3.5 and the MAD with it. An absolute ">0.9"
    # here failed for that reason, and tightening it would have been pinning an
    # arbitrary number rather than the property that matters.
    assert classic_shift < 0.5, "classic z should be badly compressed by the outlier"
    assert robust_shift > 2 * classic_shift, (
        f"robust z ({robust_shift:.3f}) should be far less compressed than "
        f"classic ({classic_shift:.3f})"
    )


def test_zero_spread_returns_zeros_not_nan():
    """A constant marker (empty channel, non-overlapping compartment) is common.
    Propagating inf/NaN would poison a gate rather than report one."""
    import numpy as np

    assert list(zscore(np.array([5.0, 5.0, 5.0]))) == [0.0, 0.0, 0.0]
    assert list(zscore(np.array([5.0, 5.0, 5.0]), robust=True)) == [0.0, 0.0, 0.0]
    assert np.isfinite(zscore(np.array([5.0, 5.0, 5.0]))).all()


def test_robust_zero_mad_with_nonzero_sd_still_returns_zeros():
    """MAD is 0 whenever >half the cells share a value, even with real spread
    elsewhere — the classic division-by-zero this guard exists for."""
    import numpy as np

    v = np.array([5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 99.0, 1000.0])
    out = zscore(v, robust=True)
    assert np.isfinite(out).all()
    assert list(out) == [0.0] * len(v)


def test_zscore_handles_an_empty_population():
    import numpy as np

    assert zscore(np.array([])).size == 0


# ── REDSEA stays whole-cell only ──────────────────────────────────────────────
def test_redsea_variants_all_derive_from_the_redsea_base():
    redsea_names = [s for s in STATISTICS if split_statistic(s)[0] == REDSEA_STATISTIC]
    assert redsea_names == ["REDSEA", "REDSEA Z", "REDSEA RobustZ"]
