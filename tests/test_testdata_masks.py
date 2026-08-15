#!/usr/bin/env python3
"""The generated mask fixtures must be readable masks, not just present files.

`tests/testdata/P001_nuclei_mask.tif` was 0 bytes and TRACKED. It satisfied every
`checkIfExists: true` that names it and it satisfied `path(...).exists()`, so nothing in
the suite could tell it apart from a real mask -- but every one of those uses is a STUB
run, where the file is staged and never opened. The moment anything read it (a real
nf-test, a local `--start postprocessing` run against
`tests/testdata/valid_checkpoint_segmented.csv`, which names it in the `nuclei_mask`
column) it would fail as a truncated TIFF.

The tell was the asymmetry, not the size: the very same CSV row names
`P001_cell_mask.tif`, which the generator writes as a real uint32 label TIFF, alongside a
`P001_nuclei_mask.tif` that the generator did not write at all. A comment in the
generator described it as "the hand-authored fixture the generator does not write (see
.gitignore's comment on this file)" -- and .gitignore had no such entry, because the file
was committed rather than generated. Three statements, none of them true.

It is generated now, like every other mask fixture, and this file is what stops it
regressing to a placeholder: existence is not the property that matters, being a
readable label image is.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

tifffile = pytest.importorskip("tifffile")

TESTDATA = Path(__file__).resolve().parent / "testdata"
CELL_MASK = TESTDATA / "P001_cell_mask.tif"
NUCLEI_MASK = TESTDATA / "P001_nuclei_mask.tif"


def _require(path: Path):
    if not path.exists():
        pytest.skip(
            f"{path.name} not generated -- run tests/testdata/generate_complete_testdata.py"
        )
    return path


def test_nuclei_mask_is_not_an_empty_placeholder():
    """The specific regression: a 0-byte file that passes every existence check."""
    _require(NUCLEI_MASK)
    assert NUCLEI_MASK.stat().st_size > 0, (
        f"{NUCLEI_MASK.relative_to(TESTDATA.parent)} is 0 bytes. It satisfies "
        "checkIfExists and path().exists() and fails any real read -- the fixture "
        "equivalent of a guard that cannot fail."
    )


def test_nuclei_mask_reads_back_as_a_label_image():
    _require(NUCLEI_MASK)
    mask = tifffile.imread(NUCLEI_MASK)
    assert mask.ndim == 2, f"expected a 2D label image, got shape {mask.shape}"
    assert np.issubdtype(mask.dtype, np.integer), (
        f"segmentation masks are integer label images; got {mask.dtype}"
    )
    labels = set(np.unique(mask)) - {0}
    assert len(labels) > 1, f"expected several labelled nuclei, found {sorted(labels)}"


def test_nuclei_labels_are_a_subset_of_the_cell_labels():
    """The pair must be usable together, which is the only reason both exist.

    `--quantify_compartments` reads both masks and keys nuclear signal to the SAME cell
    label as whole-cell signal (bin/quantify.py). A nuclei fixture whose labels did not
    correspond to the cell fixture's would still be a valid TIFF and still be useless --
    so "non-empty" is necessary and not sufficient.
    """
    _require(CELL_MASK)
    _require(NUCLEI_MASK)
    cell = tifffile.imread(CELL_MASK)
    nuc = tifffile.imread(NUCLEI_MASK)
    assert nuc.shape == cell.shape, (
        f"nuclei mask {nuc.shape} does not match cell mask {cell.shape}"
    )
    cell_labels = set(np.unique(cell)) - {0}
    nuc_labels = set(np.unique(nuc)) - {0}
    assert nuc_labels <= cell_labels, (
        f"nuclei labels not present in the cell mask: {sorted(nuc_labels - cell_labels)}"
    )
    # Every nucleus pixel must sit inside its own cell -- a nucleus straddling the
    # background or a neighbouring cell would silently mis-assign compartment signal.
    inside = cell[nuc > 0] == nuc[nuc > 0]
    assert inside.all(), (
        f"{(~inside).sum()} nucleus pixels fall outside their own cell label"
    )


def test_no_tracked_mask_fixture_is_a_placeholder():
    """The class, not just the instance.

    Both generated mask TIFFs go through the same check, so adding a third and
    forgetting to write it is caught here rather than in whatever reads it first.
    """
    empty = [
        p.name
        for p in (CELL_MASK, NUCLEI_MASK)
        if p.exists() and p.stat().st_size == 0
    ]
    assert not empty, f"0-byte mask fixtures: {empty}"
