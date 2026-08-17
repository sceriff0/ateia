"""``bin/convert_image.py:convert_to_ome_tiff`` must write the slide one plane at a time.

BOTH HALVES OR NOTHING. Swapping ``img.data`` for ``img.dask_data`` (pinned by
``tests/test_convert_lazy_read.py``) on its own saves no memory at all: the lazy handle
is handed straight to ``tifffile.imwrite``, which calls ``numpy.asarray`` on it and
materialises the whole slide at the write instead of at the read. The write has to
stream -- ``tifffile.TiffWriter(...).write(<iterator of planes>, shape=..., dtype=...)``,
the pattern ``bin/tiled_stitch.py:156-170`` already uses for the gigapixel stitch.

This file pins the write half and, crucially, the OUTPUT EQUIVALENCE: the streamed file
must be byte-identical to what the previous ``tifffile.imwrite(path, whole_array, ...)``
call produced from the same pixels and the same OME metadata.

The one byte-level caveat, measured rather than assumed: tifffile stamps a fresh
time-based UUID into the OME-XML header (``UUID="urn:uuid:..."``) on every write, so no
two OME-TIFFs tifffile ever writes are byte-identical, including two runs of the *old*
code. ``test_the_only_nondeterminism_is_the_ome_uuid`` is the control that establishes
that -- it writes the same array twice through the OLD writer and asserts the ONLY
differing bytes fall inside that UUID field. Every equivalence assertion below therefore
compares bytes with that one 36-character field masked, which is exactly as strict as
byte-for-byte can be made for this format.
"""

from __future__ import annotations

import re
import sys
import types

import dask.array as da
import numpy as np
import tifffile

from bin import convert_image

_UUID_RE = re.compile(rb"urn:uuid:[0-9a-fA-F-]{36}")
_UUID_MASK = b"urn:uuid:00000000-0000-0000-0000-000000000000"


def _mask_uuid(raw: bytes) -> bytes:
    """Blank the one field tifffile randomises per write (see the module docstring)."""
    return _UUID_RE.sub(_UUID_MASK, raw)


def _legacy_write(output_filename, image_data, ome_metadata):
    """The write this task replaces, verbatim, kept only as the equivalence reference.

    ``bin/convert_image.py`` before this change::

        tifffile.imwrite(output_filename, image_data, metadata=ome_metadata,
                         photometric="minisblack", ome=True, bigtiff=True)
    """
    tifffile.imwrite(
        output_filename,
        np.asarray(image_data),
        metadata=ome_metadata,
        photometric="minisblack",
        ome=True,
        bigtiff=True,
    )


# ---------------------------------------------------------------------------
# fixtures: drive the real convert_to_ome_tiff through a controlled read
# ---------------------------------------------------------------------------


class _RefusesWholeArray:
    """An array-like that serves 2-D planes but refuses whole-array materialisation.

    This is the tripwire that makes "the write streams" a behavioural assertion rather
    than a grep for ``TiffWriter``. ``tifffile.imwrite(path, array, ...)`` calls
    ``numpy.asarray`` on its argument; a writer that still does so trips ``__array__``
    and fails with the reason.
    """

    def __init__(self, array):
        self._array = array
        self.shape = array.shape
        self.dtype = array.dtype
        self.ndim = array.ndim
        self.planes_read = []

    def __getitem__(self, index):
        self.planes_read.append(index)
        plane = self._array[index]
        assert plane.ndim == 2, f"the writer asked for {plane.ndim}-D, not one plane"
        return plane

    def __array__(self, *_args, **_kwargs):
        raise AssertionError(
            "the writer materialised the whole stack; convert_to_ome_tiff must stream planes"
        )


class _FakeDims:
    def __init__(self, order, sizes):
        self.order = order
        self._sizes = sizes

    def __getattr__(self, name):
        sizes = self.__dict__.get("_sizes", {})
        if name in sizes:
            return sizes[name]
        raise AttributeError(name)

    def __repr__(self):
        return "<Dimensions [" + " ".join(f"{d}: {self._sizes[d]}" for d in self.order) + "]>"


class _FakePixelSizes:
    def __init__(self, x=None, y=None, z=None):
        self.X, self.Y, self.Z = x, y, z


class _FakeBioImage:
    def __init__(self, array, order, sizes, channel_names, pixel_sizes):
        self._array = array
        self.dims = _FakeDims(order, sizes)
        self.shape = array.shape
        self.channel_names = channel_names
        self.physical_pixel_sizes = pixel_sizes

    @property
    def data(self):
        raise AssertionError("BioImage.data materialises the entire slide")

    @property
    def dask_data(self):
        # One dask chunk per plane, which is what a real BioImage reader produces for a
        # planar file and what makes a per-plane write actually read per-plane.
        chunks = (1,) + self._array.shape[1:]
        return da.from_array(self._array, chunks=chunks)


def _install_fake_bioio(monkeypatch, img):
    module = types.ModuleType("bioio")
    module.BioImage = lambda _path: img
    monkeypatch.setitem(sys.modules, "bioio", module)


def _planar_bioimage(seed=11, shape=(1, 3, 1, 24, 20)):
    """``TCZYX``, three channels, nuclear marker NOT in position 0 (forces the reorder)."""
    rng = np.random.default_rng(seed)
    array = rng.integers(0, 4000, size=shape, dtype=np.uint16)
    return array, _FakeBioImage(
        array,
        order="TCZYX",
        sizes=dict(zip("TCZYX", shape)),
        channel_names=["CD3", "DAPI", "CD8"],
        pixel_sizes=_FakePixelSizes(0.325, 0.325, None),
    )


def _s_as_c_bioimage(seed=13, shape=(1, 1, 1, 24, 20, 3)):
    """``TCZYXS``: singleton ``C``, three ``S`` samples -- the RGB-ish vendor file."""
    rng = np.random.default_rng(seed)
    array = rng.integers(0, 255, size=shape, dtype=np.uint16)
    return array, _FakeBioImage(
        array,
        order="TCZYXS",
        sizes=dict(zip("TCZYXS", shape)),
        channel_names=["CD3", "DAPI", "CD8"],
        pixel_sizes=_FakePixelSizes(0.325, 0.325, None),
    )


def _convert(monkeypatch, img, out_dir, channels, writer=None):
    """Run the real ``convert_to_ome_tiff``, optionally with the legacy writer patched in."""
    _install_fake_bioio(monkeypatch, img)
    args = (out_dir / "input.czi", out_dir, "P1")
    if writer is None:
        return convert_image.convert_to_ome_tiff(*args, channel_names=list(channels))
    # A context, not a bare setattr: monkeypatch.setattr lasts the whole test, so a
    # patched reference write followed by an "unpatched" one would silently compare the
    # legacy writer against itself and pass vacuously.
    with monkeypatch.context() as patched:
        patched.setattr(convert_image, "write_ome_tiff", writer)
        return convert_image.convert_to_ome_tiff(*args, channel_names=list(channels))


# ---------------------------------------------------------------------------
# the write streams
# ---------------------------------------------------------------------------


def test_the_write_never_materialises_the_whole_stack(monkeypatch, tmp_path):
    rng = np.random.default_rng(17)
    array = rng.integers(0, 4000, size=(3, 24, 20), dtype=np.uint16)
    guard = _RefusesWholeArray(array)

    monkeypatch.setattr(
        convert_image,
        "read_image",
        lambda _path: (
            guard,
            {
                "num_channels": 3,
                "physical_pixel_size_x": 0.325,
                "physical_pixel_size_y": 0.325,
                "physical_pixel_size_z": None,
                "channel_names_from_file": ["DAPI", "CD3", "CD8"],
                "original_dims": "CYX",
            },
        ),
    )

    out, channels = convert_image.convert_to_ome_tiff(
        tmp_path / "input.czi", tmp_path, "P1", channel_names=["DAPI", "CD3", "CD8"]
    )

    assert channels == ["DAPI", "CD3", "CD8"]
    assert len(guard.planes_read) == 3, (
        f"expected one read per plane, got {guard.planes_read}"
    )
    np.testing.assert_array_equal(tifffile.imread(str(out)), array)


# ---------------------------------------------------------------------------
# output equivalence
# ---------------------------------------------------------------------------


def test_streamed_bytes_match_the_eager_writer_for_a_multichannel_stack(
    monkeypatch, tmp_path
):
    array, img = _planar_bioimage()
    channels = ["CD3", "DAPI", "CD8"]

    ref_dir = tmp_path / "ref"
    ref_dir.mkdir()
    new_dir = tmp_path / "new"
    new_dir.mkdir()

    ref_path, ref_channels = _convert(
        monkeypatch, img, ref_dir, channels, writer=_legacy_write
    )
    new_path, new_channels = _convert(monkeypatch, img, new_dir, channels)

    assert ref_channels == new_channels == ["DAPI", "CD3", "CD8"]
    assert ref_path.name == new_path.name
    assert _mask_uuid(new_path.read_bytes()) == _mask_uuid(ref_path.read_bytes())

    # ... and the pixels really are the reordered source, not merely equal to each other.
    expected = np.take(array[0, :, 0], [1, 0, 2], axis=0)
    np.testing.assert_array_equal(tifffile.imread(str(new_path)), expected)


def test_streamed_bytes_match_the_eager_writer_for_the_s_as_c_case(monkeypatch, tmp_path):
    array, img = _s_as_c_bioimage()
    channels = ["CD3", "DAPI", "CD8"]

    ref_dir = tmp_path / "ref"
    ref_dir.mkdir()
    new_dir = tmp_path / "new"
    new_dir.mkdir()

    ref_path, _ = _convert(monkeypatch, img, ref_dir, channels, writer=_legacy_write)
    new_path, _ = _convert(monkeypatch, img, new_dir, channels)

    assert _mask_uuid(new_path.read_bytes()) == _mask_uuid(ref_path.read_bytes())

    # TCZYXS -> squeeze C -> TZYXC -> transpose C in front of Y -> TCZYX -> squeeze T, Z
    expected = np.take(array[0, 0, 0].transpose(2, 0, 1), [1, 0, 2], axis=0)
    np.testing.assert_array_equal(tifffile.imread(str(new_path)), expected)


def test_the_ome_header_is_unchanged(monkeypatch, tmp_path):
    """Channel names and PhysicalSize live in the OME-XML; the streamed write must keep them."""
    _array, img = _planar_bioimage()
    channels = ["CD3", "DAPI", "CD8"]

    ref_dir = tmp_path / "ref"
    ref_dir.mkdir()
    new_dir = tmp_path / "new"
    new_dir.mkdir()

    ref_path, _ = _convert(monkeypatch, img, ref_dir, channels, writer=_legacy_write)
    new_path, _ = _convert(monkeypatch, img, new_dir, channels)

    with tifffile.TiffFile(str(ref_path)) as ref, tifffile.TiffFile(str(new_path)) as new:
        ref_xml = _mask_uuid(ref.ome_metadata.encode())
        new_xml = _mask_uuid(new.ome_metadata.encode())

    assert new_xml == ref_xml
    assert b'Name="DAPI"' in new_xml
    assert b'PhysicalSizeX="0.325"' in new_xml


def test_the_only_nondeterminism_is_the_ome_uuid(tmp_path):
    """The control for the masking above: prove the OLD writer is not byte-stable either.

    Without this, ``_mask_uuid`` would be an unexamined escape hatch -- it could be
    hiding a real difference. Two runs of the *reference* writer over identical pixels
    are compared here, and the only differing byte offsets must fall inside the masked
    UUID field. If tifffile ever becomes deterministic, this test fails and the masking
    can be deleted.
    """
    rng = np.random.default_rng(23)
    array = rng.integers(0, 4000, size=(3, 24, 20), dtype=np.uint16)
    metadata = {
        "axes": "CYX",
        "Channel": {"Name": ["DAPI", "CD3", "CD8"]},
        "PhysicalSizeX": 0.325,
        "PhysicalSizeXUnit": "µm",
        "PhysicalSizeY": 0.325,
        "PhysicalSizeYUnit": "µm",
    }

    first = tmp_path / "one.ome.tif"
    second = tmp_path / "two.ome.tif"
    _legacy_write(first, array, metadata)
    _legacy_write(second, array, metadata)

    raw_a = first.read_bytes()
    raw_b = second.read_bytes()
    assert raw_a != raw_b, (
        "tifffile now writes byte-identical OME-TIFFs; drop _mask_uuid and compare raw bytes"
    )

    differing = [i for i in range(len(raw_a)) if raw_a[i] != raw_b[i]]
    uuid_spans = [m.span() for m in _UUID_RE.finditer(raw_a)]
    assert uuid_spans, "no urn:uuid field found in the reference file"
    assert all(
        any(lo <= i < hi for lo, hi in uuid_spans) for i in differing
    ), f"bytes outside the OME UUID differ between two identical writes: {differing[:20]}"

    assert _mask_uuid(raw_a) == _mask_uuid(raw_b)


def test_the_numpy_reader_branches_also_write_identical_bytes(monkeypatch, tmp_path):
    """The NDPI/HDF5 readers still hand over a decoded ``np.ndarray``.

    They go through the same streamed writer now, so their published bytes are in scope
    too -- a change confined to the BioIO branch would still have moved them.
    """
    rng = np.random.default_rng(29)
    array = rng.integers(0, 4000, size=(3, 24, 20), dtype=np.uint16)
    metadata = {
        "num_channels": 3,
        "physical_pixel_size_x": None,
        "physical_pixel_size_y": None,
        "physical_pixel_size_z": None,
        "channel_names_from_file": None,
        "original_dims": "CYX",
    }
    monkeypatch.setattr(convert_image, "read_image", lambda _p: (array, dict(metadata)))

    ref_dir = tmp_path / "ref"
    ref_dir.mkdir()
    new_dir = tmp_path / "new"
    new_dir.mkdir()

    with monkeypatch.context() as patched:
        patched.setattr(convert_image, "write_ome_tiff", _legacy_write)
        ref_path, _ = convert_image.convert_to_ome_tiff(
            tmp_path / "in.ndpi", ref_dir, "P1", channel_names=["CD3", "DAPI", "CD8"]
        )
    new_path, _ = convert_image.convert_to_ome_tiff(
        tmp_path / "in.ndpi", new_dir, "P1", channel_names=["CD3", "DAPI", "CD8"]
    )

    assert _mask_uuid(new_path.read_bytes()) == _mask_uuid(ref_path.read_bytes())
    np.testing.assert_array_equal(
        tifffile.imread(str(new_path)), np.take(array, [1, 0, 2], axis=0)
    )
