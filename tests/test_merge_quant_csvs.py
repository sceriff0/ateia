import importlib.util
from pathlib import Path

import pandas as pd

# Import the bin script as a module (it inserts bin/utils on sys.path at import).
_SPEC = importlib.util.spec_from_file_location(
    "merge_quant_csvs",
    Path(__file__).resolve().parent.parent / "bin" / "merge_quant_csvs.py",
)
mqc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mqc)


def _write_csv(tmp_path, name, frame):
    p = tmp_path / name
    frame.to_csv(p, index=False)
    return p


def test_new_marker_appends_as_column(tmp_path):
    base = pd.DataFrame({"label": [1, 2, 3], "area": [10, 20, 30]})
    new = _write_csv(
        tmp_path,
        "c2_CD8_quant.csv",
        pd.DataFrame({"label": [1, 2, 3], "CD8": [5.0, 6.0, 7.0]}),
    )
    out = mqc.merge_intensities(base, [new])
    assert list(out["CD8"]) == [5.0, 6.0, 7.0]
    assert len(out) == 3  # no cells gained/lost


def test_colliding_marker_new_cycle_wins(tmp_path):
    # Base already has a PANCK column (from cycle 1); new cycle re-measures PANCK.
    base = pd.DataFrame({"label": [1, 2], "area": [10, 20], "PANCK": [1.0, 2.0]})
    new = _write_csv(
        tmp_path,
        "c2_PANCK_quant.csv",
        pd.DataFrame({"label": [1, 2], "PANCK": [99.0, 98.0]}),
    )
    out = mqc.merge_intensities(base, [new])
    # New cycle overwrites; no pandas _x/_y suffix columns survive.
    assert "PANCK_x" not in out.columns and "PANCK_y" not in out.columns
    assert list(out["PANCK"]) == [99.0, 98.0]


def test_dapi_is_protected_from_overwrite(tmp_path):
    base = pd.DataFrame({"label": [1, 2], "DAPI": [100.0, 200.0]})
    new = _write_csv(
        tmp_path,
        "c2_DAPI_quant.csv",
        pd.DataFrame({"label": [1, 2], "DAPI": [1.0, 2.0]}),
    )
    out = mqc.merge_intensities(base, [new], protected_cols=("DAPI",))
    # Base DAPI kept, incoming ignored.
    assert list(out["DAPI"]) == [100.0, 200.0]


def test_reorder_columns_preserves_prior_cell_size_without_area():
    """Regression (add_cycle path): a prior merged table can carry fov/cell_size
    without an ``area`` column (e.g. an older schema, or cell_size computed some
    other way). Before adopting the canonical 12-entry MORPHOLOGY_COLS (which
    includes fov/cell_size), a hardcoded ``col not in ("fov", "cell_size")``
    clause excluded them from BOTH morpho_present and marker_cols_all, so the
    `merged[final_column_order]` column selection dropped them entirely.
    cell_size was only ever recreated from `area` a few lines later, so a prior
    cell_size with no matching area column was silently lost forever.

    With the canonical 12-entry list, fov/cell_size are ordinary morphology
    columns and survive the selection unconditionally.
    """
    prior = pd.DataFrame(
        {
            "label": [1, 2, 3],
            "fov": ["prior_patient"] * 3,
            "cell_size": [111.0, 222.0, 333.0],
            "DAPI": [1.0, 2.0, 3.0],
        }
    )
    out = mqc.reorder_columns(prior.copy(), patient_id="cycle2_patient")

    # cell_size survives from the base table exactly -- no `area` column
    # existed, so it is never overwritten/recomputed. This is the bug fix.
    assert "cell_size" in out.columns
    assert list(out["cell_size"]) == [111.0, 222.0, 333.0]

    # fov is still (unconditionally, unrelated to this change) refreshed to
    # the current patient_id -- required for Pixie fov tagging.
    assert list(out["fov"]) == ["cycle2_patient"] * 3


# ---------------------------------------------------------------------------
# The nuclear/fiducial marker is whatever params.nuclear_markers says it is.
# Both behaviours below used to key off the literal string "DAPI", so a panel
# whose fiducial is CELLTOX silently lost the protection and the ordering.
# ---------------------------------------------------------------------------


def test_reorder_puts_the_configured_fiducial_first_not_literal_dapi():
    """CELLTOX leads the marker block when it is the configured fiducial.

    Without the marker list it sorts alphabetically -- CD3 would come first --
    which is what the hardcoded "DAPI" check produced for every non-DAPI panel.
    """
    df = pd.DataFrame(
        {"label": [1, 2], "CD3": [1.0, 2.0], "CELLTOX": [3.0, 4.0], "PANCK": [5.0, 6.0]}
    )
    out = mqc.reorder_columns(df.copy(), "p1", nuclear_markers=["CELLTOX"])
    markers = [c for c in out.columns if c in ("CD3", "CELLTOX", "PANCK")]
    assert markers == ["CELLTOX", "CD3", "PANCK"], markers


def test_reorder_still_puts_dapi_first_under_the_shipped_default():
    """The shipped default must not regress: DAPI still leads for a DAPI panel."""
    df = pd.DataFrame({"label": [1], "CD3": [1.0], "DAPI": [2.0], "PANCK": [3.0]})
    out = mqc.reorder_columns(df.copy(), "p1", nuclear_markers=["DAPI", "CELLTOX"])
    markers = [c for c in out.columns if c in ("CD3", "DAPI", "PANCK")]
    assert markers == ["DAPI", "CD3", "PANCK"], markers


def test_a_marker_outside_the_configured_list_does_not_lead():
    """DAPI is an ordinary marker when it is not the configured fiducial."""
    df = pd.DataFrame({"label": [1], "CD3": [1.0], "DAPI": [2.0]})
    out = mqc.reorder_columns(df.copy(), "p1", nuclear_markers=["CELLTOX"])
    markers = [c for c in out.columns if c in ("CD3", "DAPI")]
    assert markers == ["CD3", "DAPI"], markers


def test_configured_fiducial_is_protected_from_overwrite(tmp_path):
    """The prior cycle's fiducial column wins over an incoming one.

    protected_cols is now derived from the marker list by the caller, so this
    pins the mechanism the CLI wires up: a protected column keeps the BASE value
    and drops the incoming one, while an unprotected marker is overwritten.
    """
    base = pd.DataFrame({"label": [1, 2], "CELLTOX": [10.0, 20.0], "CD3": [1.0, 2.0]})
    incoming = tmp_path / "new_quant.csv"
    pd.DataFrame({"label": [1, 2], "CELLTOX": [99.0, 99.0], "CD3": [7.0, 8.0]}).to_csv(
        incoming, index=False
    )

    out = mqc.merge_intensities(base.copy(), [incoming], protected_cols=("CELLTOX",))

    assert list(out["CELLTOX"]) == [10.0, 20.0], "protected fiducial was overwritten"
    assert list(out["CD3"]) == [7.0, 8.0], "unprotected marker was not overwritten"


# ---------------------------------------------------------------------------
# Characterisation of merge_intensities, written before PERF-PLAN 1.4 replaces
# the N sequential merges with one concat (measured 2.05x / -274 MB).
#
# These pin the behaviour the refactor must preserve. The subtle one is
# FILE-vs-FILE collision: the loop mutates `merged` as it goes, so by the time
# the second file is considered its marker is already in the base. A deferred
# concat that only checks the ORIGINAL base columns would silently change both
# the overwrite and the protection outcome, and no existing test covered it.
# ---------------------------------------------------------------------------


def test_a_later_file_overwrites_an_earlier_file_s_marker(tmp_path):
    base = pd.DataFrame({"label": [1, 2], "area": [10.0, 20.0]})
    first = _write_csv(
        tmp_path, "a.csv", pd.DataFrame({"label": [1, 2], "CD3": [100, 200]})
    )
    second = _write_csv(
        tmp_path, "b.csv", pd.DataFrame({"label": [1, 2], "CD3": [999, 888]})
    )

    out = mqc.merge_intensities(base, [first, second])

    assert list(out["CD3"]) == [999, 888]
    assert list(out.columns).count("CD3") == 1


def test_protection_keeps_the_first_file_s_marker_against_a_later_file(tmp_path):
    """Protection is evaluated against the running table, so file 1 wins, not the base."""
    base = pd.DataFrame({"label": [1, 2], "area": [10.0, 20.0]})
    first = _write_csv(
        tmp_path, "a.csv", pd.DataFrame({"label": [1, 2], "CD3": [100, 200]})
    )
    second = _write_csv(
        tmp_path, "b.csv", pd.DataFrame({"label": [1, 2], "CD3": [999, 888]})
    )

    out = mqc.merge_intensities(base, [first, second], protected_cols=("CD3",))

    assert list(out["CD3"]) == [100, 200]


def test_a_cell_absent_from_an_intensity_file_becomes_nan(tmp_path):
    """`how="left"` semantics: the base keeps every cell, missing values are NaN."""
    base = pd.DataFrame({"label": [1, 2, 3], "area": [10.0, 20.0, 30.0]})
    partial = _write_csv(
        tmp_path, "a.csv", pd.DataFrame({"label": [1, 3], "CD3": [5.0, 7.0]})
    )

    out = mqc.merge_intensities(base, [partial])

    assert len(out) == 3
    assert list(out["label"]) == [1, 2, 3]
    assert out.loc[out["label"] == 2, "CD3"].isna().all()
    assert out.loc[out["label"] == 3, "CD3"].iloc[0] == 7.0


def test_extra_cells_in_an_intensity_file_are_dropped(tmp_path):
    base = pd.DataFrame({"label": [1, 2], "area": [10.0, 20.0]})
    extra = _write_csv(
        tmp_path, "a.csv", pd.DataFrame({"label": [1, 2, 99], "CD3": [5.0, 6.0, 7.0]})
    )

    out = mqc.merge_intensities(base, [extra])

    assert list(out["label"]) == [1, 2]
    assert 99 not in set(out["label"])


def test_column_order_follows_file_order(tmp_path):
    base = pd.DataFrame({"label": [1], "area": [10.0]})
    f1 = _write_csv(tmp_path, "a.csv", pd.DataFrame({"label": [1], "CD3": [1.0]}))
    f2 = _write_csv(tmp_path, "b.csv", pd.DataFrame({"label": [1], "CD8": [2.0]}))

    out = mqc.merge_intensities(base, [f1, f2])

    assert list(out.columns) == ["label", "area", "CD3", "CD8"]


def test_a_file_with_no_marker_columns_is_skipped(tmp_path):
    base = pd.DataFrame({"label": [1], "area": [10.0]})
    empty = _write_csv(tmp_path, "a.csv", pd.DataFrame({"label": [1]}))

    out = mqc.merge_intensities(base, [empty])

    assert list(out.columns) == ["label", "area"]


def test_base_row_order_is_preserved_even_if_the_file_is_shuffled(tmp_path):
    base = pd.DataFrame({"label": [3, 1, 2], "area": [30.0, 10.0, 20.0]})
    shuffled = _write_csv(
        tmp_path, "a.csv", pd.DataFrame({"label": [2, 3, 1], "CD3": [22.0, 33.0, 11.0]})
    )

    out = mqc.merge_intensities(base, [shuffled])

    assert list(out["label"]) == [3, 1, 2]
    assert list(out["CD3"]) == [33.0, 11.0, 22.0]


def test_a_duplicate_label_is_refused_rather_than_silently_duplicating_cells(tmp_path):
    """DELIBERATE DIVERGENCE from the old sequential merge.

    `merged.merge(..., on="label", how="left")` with a duplicated label in the incoming file
    duplicated every matching base row, silently inflating the cell count for everything
    downstream. The reindex cannot reproduce that, and reproducing it would be wrong: one row
    per cell is the contract for a per-cell quantification CSV.
    """
    import pytest

    base = pd.DataFrame({"label": [1, 2], "area": [10.0, 20.0]})
    dupes = _write_csv(
        tmp_path, "a.csv", pd.DataFrame({"label": [1, 1, 2], "CD3": [5.0, 6.0, 7.0]})
    )

    with pytest.raises(ValueError, match="duplicate label"):
        mqc.merge_intensities(base, [dupes])
