"""Unit tests for bin/utils/ome_io.py -- the pipeline's one image I/O module.

WHY THE IMPORTS BELOW LOOK LIKE THIS. `ome_io` is a bin/utils module, and the
convention every bin/ script follows is `sys.path.insert(0, .../bin/utils)`
followed by a flat `from ome_io import ...`. Both inserts are needed here (bin
for `from bin import ...` style helpers elsewhere, bin/utils for the flat
sibling imports ome_io itself performs), matching tests/test_tiled_io.py.

WHAT IS DELIBERATELY NOT TESTED HERE. `bioio`, `bioio-bioformats` and `h5py`
are installed in NO CI environment (requirements/ci.txt installs none of the
three), so every test in this file must run without them. That is exactly why
ome_io imports all three lazily, inside functions: importing this module must
never require them. Plan 07 writes the format-specific tests on synthesised
fixtures; this file tests ome_io itself.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
)
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "utils"
    ),
)
tifffile = pytest.importorskip("tifffile")

import ome_io  # noqa: E402

# ---------------------------------------------------------------------------
# detect_reader -- the format dispatch table
# ---------------------------------------------------------------------------

BIOIO = [".ome.tif", ".ome.tiff", ".tif", ".tiff", ".nd2", ".czi", ".lif"]
TIFFFILE = [".ndpi", ".ndpis"]
HDF5 = [".h5", ".hdf5"]
BIOFORMATS = [".svs", ".qptiff", ".vsi", ".scn", ".mrxs", ".bif", ".ims"]


@pytest.mark.parametrize("suffix", BIOIO)
def test_bioio_native_suffixes(suffix):
    assert ome_io.detect_reader(f"/x/slide{suffix}") == "bioio"


@pytest.mark.parametrize("suffix", TIFFFILE)
def test_hamamatsu_suffixes_go_to_tifffile(suffix):
    assert ome_io.detect_reader(f"/x/slide{suffix}") == "tifffile"


@pytest.mark.parametrize("suffix", HDF5)
def test_hdf5_suffixes(suffix):
    assert ome_io.detect_reader(f"/x/slide{suffix}") == "hdf5"


@pytest.mark.parametrize("suffix", BIOFORMATS)
def test_bioformats_only_suffixes(suffix):
    assert ome_io.detect_reader(f"/x/slide{suffix}") == "bioio-bioformats"


def test_every_answer_is_one_of_READERS():
    for suffix in BIOIO + TIFFFILE + HDF5 + BIOFORMATS:
        assert ome_io.detect_reader(f"/x/s{suffix}") in ome_io.READERS


def test_the_dispatch_is_case_insensitive():
    assert ome_io.detect_reader("/x/SLIDE.SVS") == "bioio-bioformats"
    assert ome_io.detect_reader("/x/SLIDE.OME.TIFF") == "bioio"


def test_an_unclaimed_suffix_raises_and_names_it():
    """The whole point of replacing convert_image.get_file_format's silent
    `return "bioio"` fall-through: a .png used to be handed to BioImage and
    failed several frames deep, naming the wrong problem."""
    with pytest.raises(ome_io.UnsupportedFormatError) as exc:
        ome_io.detect_reader("/x/thumbnail.png")
    message = str(exc.value)
    assert ".png" in message, "the message must name the suffix that was refused"
    assert ".czi" in message, "the message must list what IS supported"


def test_a_file_with_no_suffix_raises():
    with pytest.raises(ome_io.UnsupportedFormatError):
        ome_io.detect_reader("/x/slide")


def test_UnsupportedFormatError_is_a_ValueError():
    """Callers that already catch ValueError around a read keep working."""
    assert issubclass(ome_io.UnsupportedFormatError, ValueError)


# ---------------------------------------------------------------------------
# require_reader -- the plugin precondition
# ---------------------------------------------------------------------------


def test_require_reader_accepts_a_reader_whose_modules_are_installed():
    ome_io.require_reader("tifffile")  # must not raise


def test_require_reader_names_the_missing_plugin():
    """`.svs` routes to bioio-bioformats, which containers/convert installs in
    phase 06 and nothing installs before it. The failure has to say the plugin's
    name, not `ModuleNotFoundError: bioio_bioformats` several frames deep."""
    if ome_io._module_is_importable("bioio_bioformats"):
        pytest.skip("bioio-bioformats is installed here; nothing to assert")
    with pytest.raises(ImportError) as exc:
        ome_io.require_reader("bioio-bioformats")
    assert "bioio-bioformats" in str(exc.value)


def test_require_reader_rejects_an_unknown_reader_name():
    with pytest.raises(ome_io.UnsupportedFormatError):
        ome_io.require_reader("openslide")
