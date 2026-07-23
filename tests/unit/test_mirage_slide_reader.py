"""MirageVipsSlideReader must round-trip mirage's own OME-TIFF writer exactly.

Runs inside mirage-valis:1.0.0 (needs pyvips + tifffile). Skips under a plain
python env (CI Python job) that lacks pyvips.
"""
import os
import sys
import tempfile

import numpy as np

try:
    import pytest
    pytest.importorskip("pyvips")
    pytest.importorskip("tifffile")
except ImportError:  # stdlib __main__ runner in-image (not under pytest)
    pass

import tifffile  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin", "utils"))

H, W, C = 300, 384, 4


def _make_slide(dirpath):
    """Write a fixture with bin/preprocess.py's exact settings. Returns (path, arr)."""
    rng = np.random.default_rng(7)
    arr = rng.integers(0, 65535, size=(C, H, W), dtype=np.uint16)
    p = os.path.join(dirpath, "s.ome.tiff")
    tifffile.imwrite(
        p, arr,
        photometric="minisblack",
        metadata={"axes": "CYX", "Channel": {"Name": ["DAPI", "SMA", "PANCK", "CD3"]}},
        bigtiff=True, ome=True, compression="zlib", tile=(256, 256),
    )
    return p, arr


def test_can_read_accepts_mirage_output():
    from mirage_slide_reader import MirageVipsSlideReader
    path, _ = _make_slide(tempfile.mkdtemp())
    assert MirageVipsSlideReader.can_read(path) is True


def test_slide2vips_matches_source_array():
    from mirage_slide_reader import MirageVipsSlideReader
    path, arr = _make_slide(tempfile.mkdtemp())
    img = MirageVipsSlideReader(path).slide2vips(level=0)
    assert (img.width, img.height, img.bands) == (W, H, C)
    got = np.frombuffer(img.write_to_memory(), dtype=np.uint16).reshape(H, W, C)
    assert np.array_equal(got, np.moveaxis(arr, 0, -1))


def test_slide2vips_region_matches_full_crop():
    from mirage_slide_reader import MirageVipsSlideReader
    path, arr = _make_slide(tempfile.mkdtemp())
    r = MirageVipsSlideReader(path)
    region = r.slide2vips(level=0, xywh=(10, 20, 64, 48))
    got = np.frombuffer(region.write_to_memory(), dtype=np.uint16).reshape(48, 64, C)
    assert np.array_equal(got, np.moveaxis(arr, 0, -1)[20:68, 10:74, :])


def test_metadata_reports_dims_and_channels():
    from mirage_slide_reader import MirageVipsSlideReader
    path, _ = _make_slide(tempfile.mkdtemp())
    md = MirageVipsSlideReader(path).metadata
    # VALIS contracts slide_dimensions as an ndarray of shape (n_levels, 2); mirage's OME-TIFFs
    # are single-level, so shape must be (1, 2).
    assert isinstance(md.slide_dimensions, np.ndarray)
    assert md.slide_dimensions.shape == (1, 2)
    assert tuple(md.slide_dimensions[0]) == (W, H)
    assert md.n_channels == C
    assert md.channel_names == ["DAPI", "SMA", "PANCK", "CD3"]
    assert md.is_rgb is False
    assert md.pyvips_interpretation == "multiband"


def test_can_read_rejects_untiled():
    from mirage_slide_reader import MirageVipsSlideReader
    dirpath = tempfile.mkdtemp()
    p = os.path.join(dirpath, "flat.tiff")
    tifffile.imwrite(p, np.zeros((32, 32), dtype=np.uint16))
    assert MirageVipsSlideReader.can_read(p) is False


def _minimal_ome_xml(w, h, c, channel_names):
    """Mirrors bin/reg_finalize.py::_minimal_ome_xml (channel names + SizeC only)."""
    chans = "".join(
        f'<Channel ID="Channel:0:{i}" Name="{n}" SamplesPerPixel="1"/>'
        for i, n in enumerate(channel_names))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<OME xmlns="http://www.openmicroscopy.org/Schemas/OME/2016-06">'
        '<Image ID="Image:0" Name="registered">'
        f'<Pixels ID="Pixels:0" DimensionOrder="XYCZT" Type="uint16" '
        f'Interleaved="false" SizeX="{w}" SizeY="{h}" SizeC="{c}" SizeZ="1" SizeT="1">'
        f'{chans}'
        '</Pixels></Image></OME>')


def _make_pyvips_slide(dirpath, c):
    """Write a fixture with bin/reg_finalize.py::_save_ome_pyvips's EXACT convention:
    bands joined vertically into one page, page-height set explicitly, tiled BigTIFF.
    Returns (path, arr) with arr shaped (H, W, c) (or (H, W) when c == 1) to match what
    slide2vips/slide2image hand back."""
    import pyvips

    rng = np.random.default_rng(11)
    arr = rng.integers(0, 65535, size=(H, W, c), dtype=np.uint16)
    channel_names = [f"chan{i}" for i in range(c)]
    ome_xml = _minimal_ome_xml(W, H, c, channel_names)

    if c == 1:
        page = pyvips.Image.new_from_array(arr[..., 0])
    else:
        bands = [pyvips.Image.new_from_array(arr[..., i]) for i in range(c)]
        page = bands[0]
        for b in bands[1:]:
            page = page.join(b, "vertical")
    page = page.copy()
    page.set_type(pyvips.GValue.gint_type, "page-height", H)
    page.set_type(pyvips.GValue.gstr_type, "image-description", ome_xml)

    p = os.path.join(dirpath, "warped.ome.tiff")
    page.tiffsave(p, tile=True, tile_width=64, tile_height=64, bigtiff=True)
    return p, (arr[..., 0] if c == 1 else arr)


def test_pyvips_writer_multichannel_can_read_and_roundtrips():
    """Exercises the reg_finalize._save_ome_pyvips convention (vertical band-join,
    explicit page-height), untested until now even though add_cycle feeds slides written
    by exactly this writer back into registration."""
    from mirage_slide_reader import MirageVipsSlideReader
    path, arr = _make_pyvips_slide(tempfile.mkdtemp(), c=4)
    assert MirageVipsSlideReader.can_read(path) is True
    reader = MirageVipsSlideReader(path)
    img = reader.slide2vips(level=0)
    assert (img.width, img.height, img.bands) == (W, H, 4)
    got = np.frombuffer(img.write_to_memory(), dtype=np.uint16).reshape(H, W, 4)
    assert np.array_equal(got, arr)


def test_pyvips_writer_single_channel_can_read_and_roundtrips():
    """The C == 1 case of the reg_finalize._save_ome_pyvips convention: no band-join at
    all, just a single tiled page with page-height == full height."""
    from mirage_slide_reader import MirageVipsSlideReader
    path, arr = _make_pyvips_slide(tempfile.mkdtemp(), c=1)
    assert MirageVipsSlideReader.can_read(path) is True
    reader = MirageVipsSlideReader(path)
    img = reader.slide2vips(level=0)
    assert (img.width, img.height, img.bands) == (W, H, 1)
    got = np.frombuffer(img.write_to_memory(), dtype=np.uint16).reshape(H, W)
    assert np.array_equal(got, arr)
    assert reader.metadata.pyvips_interpretation == "b-w"


def test_can_read_rejects_non_bigtiff():
    """Isolates the ``tf.is_bigtiff`` predicate: tiled, single-sample, OME, but written as
    plain (non-Big) TIFF. Every other predicate (tiled, non-pyramidal, samplesperpixel==1,
    OME-XML present, SizeC matching) holds -- only is_bigtiff is False."""
    from mirage_slide_reader import MirageVipsSlideReader
    dirpath = tempfile.mkdtemp()
    p = os.path.join(dirpath, "notbig.ome.tiff")
    rng = np.random.default_rng(7)
    arr = rng.integers(0, 65535, size=(C, H, W), dtype=np.uint16)
    tifffile.imwrite(
        p, arr,
        photometric="minisblack",
        metadata={"axes": "CYX", "Channel": {"Name": ["DAPI", "SMA", "PANCK", "CD3"]}},
        bigtiff=False, ome=True, compression="zlib", tile=(256, 256),
    )
    assert MirageVipsSlideReader.can_read(p) is False


def test_can_read_rejects_rgb_interleaved():
    """Isolates the ``samplesperpixel == 1`` predicate: tiled BigTIFF OME-TIFF, but written
    as an interleaved RGB image (samplesperpixel == 3). Tiled/BigTIFF/non-pyramidal/OME-XML
    predicates all hold -- only samplesperpixel disqualifies it."""
    from mirage_slide_reader import MirageVipsSlideReader
    dirpath = tempfile.mkdtemp()
    p = os.path.join(dirpath, "rgb.ome.tiff")
    rng = np.random.default_rng(7)
    rgb_arr = rng.integers(0, 255, size=(H, W, 3), dtype=np.uint8)
    tifffile.imwrite(
        p, rgb_arr,
        photometric="rgb",
        bigtiff=True, ome=True, compression="zlib", tile=(256, 256),
    )
    assert MirageVipsSlideReader.can_read(p) is False


def test_can_read_rejects_pyramidal():
    """Isolates the "no sub-IFDs" predicate: tiled BigTIFF OME-TIFF, single-sample, but with
    a reduced-resolution sub-image written via ``subifds=``/``subfiletype=1`` so page 0 has a
    non-empty ``subifds``. Tiled/BigTIFF/samplesperpixel/OME-XML predicates all hold -- only
    the subifds check disqualifies it."""
    from mirage_slide_reader import MirageVipsSlideReader
    dirpath = tempfile.mkdtemp()
    p = os.path.join(dirpath, "pyramidal.ome.tiff")
    rng = np.random.default_rng(7)
    arr = rng.integers(0, 65535, size=(C, H, W), dtype=np.uint16)
    with tifffile.TiffWriter(p, bigtiff=True, ome=True) as tif:
        options = dict(photometric="minisblack", tile=(256, 256), compression="zlib")
        tif.write(
            arr, subifds=1,
            metadata={"axes": "CYX", "Channel": {"Name": ["DAPI", "SMA", "PANCK", "CD3"]}},
            **options,
        )
        # Reduced-resolution level, appended as the declared subifd.
        tif.write(arr[:, ::2, ::2], subfiletype=1, **options)

    with tifffile.TiffFile(p) as tf:
        assert tf.pages[0].subifds, "fixture did not actually produce a pyramidal page 0"

    assert MirageVipsSlideReader.can_read(p) is False


def test_can_read_rejects_sizec_mismatch():
    """Isolates the OME ``SizeC`` reconciliation predicate: a tiled BigTIFF OME-TIFF written
    with reg_finalize's own pyvips convention (vertical band-join, explicit page-height), but
    whose embedded OME-XML declares ``SizeC`` that disagrees with the actual number of
    reconstructed bands. Tiled/BigTIFF/non-pyramidal/samplesperpixel/OME-XML-present
    predicates all hold -- only the SizeC reconciliation disqualifies it."""
    from mirage_slide_reader import MirageVipsSlideReader
    import pyvips

    dirpath = tempfile.mkdtemp()
    p = os.path.join(dirpath, "sizec_mismatch.ome.tiff")
    c = C
    claimed_sizec = C - 1  # deliberately wrong
    rng = np.random.default_rng(11)
    arr = rng.integers(0, 65535, size=(H, W, c), dtype=np.uint16)
    ome_xml = _minimal_ome_xml(W, H, claimed_sizec, [f"chan{i}" for i in range(c)])

    bands = [pyvips.Image.new_from_array(arr[..., i]) for i in range(c)]
    page = bands[0]
    for b in bands[1:]:
        page = page.join(b, "vertical")
    page = page.copy()
    page.set_type(pyvips.GValue.gint_type, "page-height", H)
    page.set_type(pyvips.GValue.gstr_type, "image-description", ome_xml)
    page.tiffsave(p, tile=True, tile_width=64, tile_height=64, bigtiff=True)

    assert MirageVipsSlideReader.can_read(p) is False


if __name__ == "__main__":
    # stdlib runner so this works in the VALIS image without pytest installed
    import traceback
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(1 if failed else 0)
