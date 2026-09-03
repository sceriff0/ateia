"""CONVERT_IMAGE's science path, run for real, on every format the generator writes.

RUNS ONLY IN THE `format-tests` CI JOB (see requirements/format-tests.txt).
tests/integration/ is excluded from the standard pytest invocation, and that
exclusion is load-bearing here: bioio + bioio-ome-tiff at containers/convert's
pins cannot coexist with the harmonised tifffile the main suite runs on. The
refusals that need NO reader live in tests/unit/test_convert_validation.py,
which does run in the main suite.

WHY A PYTEST RATHER THAN AN nf-test. `-stub` never evaluates a `script:` block,
so a stub CONVERT_IMAGE proves channel wiring and nothing about the conversion:
its `stub:` block is `touch ${prefix}.ome.tif`, an EMPTY file that read_info
cannot open. A real nf-test needs the container, which does not run on arm64 and
belongs to the nightly/cluster suites. Calling main() here runs the real read,
the real channel reordering and the real streamed write, in the reader stack the
image ships, with no Docker.

THE OUTPUT IS ASSERTED THROUGH ome_io.read_info, never by re-opening it with
tifffile. read_info is the published interface; a test that reached past it
would pin bin/convert_image.py's internals instead of its behaviour. The one
exception is `_planes_of`, which reads the SOURCE fixture -- an input the
converter did not write -- to check that pixels moved with their names.

ARGV IS BUILT TO MATCH modules/local/convert_image.nf's RENDERED COMMAND, flag
for flag: --input_file, --output_dir, --patient_id, --channels, --pixel_size,
--nuclear-markers. The process passes --nuclear-markers unconditionally (through
MarkerUtils, for the reason its own comment records), so `_convert` does too --
relying on argparse's default here would exercise an argv shape the pipeline
never renders. The one deliberate exception is --channels: the three cases
parametrized with channels=None (fmt_pyramid, fmt_bigtiff, fmt_image.h5) omit
it on purpose, to exercise convert_image.py's channel_names_from_file fallback
-- the path a real run takes whenever the samplesheet's `channels` column is
absent and the reader's own metadata must supply the names instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

# parents[0]=formats, [1]=integration, [2]=tests, [3]=repo root. Matches
# test_ome_io_read_info.py: ome_io imports its bin/utils siblings flat, so both
# `bin` and `bin/utils` must be on sys.path -- not `from utils import ome_io`.
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "bin" / "utils"))

import convert_image  # noqa: E402
import ome_io  # noqa: E402
import tiled_io  # noqa: E402

#: nextflow.config:132's `nuclear_markers` default, i.e. what MarkerUtils renders
#: into CONVERT_IMAGE's command line on a run that overrides nothing.
PIPELINE_NUCLEAR_MARKERS = ["DAPI", "CELLTOX"]


def _fixture(rel: str) -> Path:
    """`rel` is the REPO-RELATIVE LITERAL, e.g. `"tests/testdata/fmt_rgb.tiff"` --
    not a bare filename joined onto a `TESTDATA` constant at call time.
    tests/test_fixtures_have_a_producer.py's `_REF` regex greps for the literal
    substring `tests/testdata/<name>` in THIS FILE'S OWN SOURCE TEXT; a
    `TESTDATA / name` join built from a bare filename never renders that substring
    anywhere on disk, so the guard cannot see these fixtures at all."""
    assert rel.startswith("tests/testdata/"), (
        f"{rel!r} must be the repo-relative literal 'tests/testdata/<name>', not a bare "
        "filename -- see this function's docstring for why."
    )
    path = ROOT / rel
    assert path.exists(), (
        f"{path} is missing. Run `python tests/testdata/generate_complete_testdata.py` "
        "-- tests/testdata/ is gitignored, so every fixture exists only because the "
        "generator wrote it."
    )
    return path


def _convert(monkeypatch, tmp_path, fixture, channels=None, pixel_size="auto"):
    """Run convert_image.main() over one fixture; return (exit code, output dir)."""
    argv = [
        "convert_image.py",
        "--input_file",
        str(_fixture(fixture)),
        "--output_dir",
        str(tmp_path),
        "--patient_id",
        "P001",
        "--pixel_size",
        pixel_size,
    ]
    if channels is not None:
        argv += ["--channels", channels]
    argv += ["--nuclear-markers", *PIPELINE_NUCLEAR_MARKERS]
    monkeypatch.setattr("sys.argv", argv)
    return convert_image.main(), tmp_path


def _planes_of(path, count):
    """Every plane of `path`, through read_plane -- the reader the QC and tiled
    paths use, and the one that had to learn the S-as-C remap for an interleaved
    source to be comparable at all."""
    return [ome_io.read_plane(path, c) for c in range(count)]


@pytest.mark.parametrize(
    "fixture,channels,basename,shape,dtype,pixel_size",
    [
        # fmt_pyramid is the only fixture whose levels differ (256 vs 128): a
        # converter that read the wrong pyramid level shows up in the shape here.
        (
            "tests/testdata/fmt_pyramid.ome.tiff",
            None,
            "P001_DAPI_CD3.ome.tif",
            (2, 256, 256),
            "uint16",
            0.5,
        ),
        (
            "tests/testdata/fmt_bigtiff.ome.tiff",
            None,
            "P001_DAPI_CD3.ome.tif",
            (2, 64, 64),
            "uint16",
            0.5,
        ),
        # .ndpis is a MANIFEST: two separate .ndpi files stacked into one C axis,
        # and the 0.5 um comes from their XResolution/ResolutionUnit tags, not OME.
        (
            "tests/testdata/fmt_set.ndpis",
            "DAPI,PANCK",
            "P001_DAPI_PANCK.ome.tif",
            (2, 64, 64),
            "uint16",
            0.5,
        ),
        # HDF5 carries its scale in an `element_size_um` ATTRIBUTE -- a third,
        # entirely separate place a pixel size can come from.
        (
            "tests/testdata/fmt_image.h5",
            None,
            "P001_DAPI_PANCK_SMA.ome.tif",
            (3, 64, 64),
            "uint16",
            0.5,
        ),
        # The three 1.0 values are NOT a default. A plain TIFF with no OME
        # PhysicalSize still carries XResolution (1, 1), which bioio reports as
        # 1.0 -- the file's own claim, which `auto` resolves rather than failing
        # on. If `auto` ever started inventing params.pixel_size here instead,
        # these would read 0.325 and nothing else would notice.
        (
            "tests/testdata/fmt_rgb.tiff",
            "DAPI,PANCK,SMA",
            "P001_DAPI_PANCK_SMA.ome.tif",
            (3, 64, 64),
            "uint8",
            1.0,
        ),
        (
            "tests/testdata/fmt_gray8.tiff",
            "DAPI",
            "P001_DAPI.ome.tif",
            (1, 64, 64),
            "uint8",
            1.0,
        ),
        (
            "tests/testdata/fmt_float32.tiff",
            "DAPI",
            "P001_DAPI.ome.tif",
            (1, 64, 64),
            "float32",
            1.0,
        ),
    ],
)
def test_every_synthesised_format_converts_to_a_readable_ome_tiff(
    monkeypatch, tmp_path, fixture, channels, basename, shape, dtype, pixel_size
):
    code, outdir = _convert(monkeypatch, tmp_path, fixture, channels)
    assert code == 0, f"convert_image.main() returned {code} for {fixture}"

    out = outdir / basename
    assert out.exists(), (
        f"{fixture} produced {sorted(p.name for p in outdir.iterdir())}, not {basename}. "
        "The output name is patient_id + the OUTPUT channel order, so a wrong name "
        "means the nuclear-first reorder moved something it should not have."
    )

    info = ome_io.read_info(out)
    assert info.shape_cyx == shape
    assert info.dtype == np.dtype(dtype)
    assert info.pixel_size_um == pytest.approx(pixel_size)
    assert info.channels is not None
    assert list(info.channels) == basename[len("P001_") : -len(".ome.tif")].split("_")

    # Every source becomes a PLANAR (C, Y, X) OME-TIFF. This is what lets the four
    # open_lazy consumers (tiled_stitch, merge_channels_pyramid, registration,
    # qc.py) treat axis 0 as the channel axis without an S-axis check of their own.
    assert info.channels_are_samples is False, (
        f"{fixture} converted to a file read_info still calls interleaved. Every "
        "downstream reader indexes (C, Y, X) directly."
    )


@pytest.mark.parametrize(
    "fixture,channels,basename,channel_count",
    [
        ("tests/testdata/fmt_pyramid.ome.tiff", None, "P001_DAPI_CD3.ome.tif", 2),
        (
            "tests/testdata/fmt_set.ndpis",
            "DAPI,PANCK",
            "P001_DAPI_PANCK.ome.tif",
            2,
        ),
        ("tests/testdata/fmt_image.h5", None, "P001_DAPI_PANCK_SMA.ome.tif", 3),
        (
            "tests/testdata/fmt_rgb.tiff",
            "DAPI,PANCK,SMA",
            "P001_DAPI_PANCK_SMA.ome.tif",
            3,
        ),
        ("tests/testdata/fmt_gray8.tiff", "DAPI", "P001_DAPI.ome.tif", 1),
        ("tests/testdata/fmt_float32.tiff", "DAPI", "P001_DAPI.ome.tif", 1),
    ],
)
def test_the_converted_slide_opens_through_the_readers_the_next_steps_use(
    monkeypatch, tmp_path, fixture, channels, basename, channel_count
):
    """read_info alone would not catch a file that PARSES but cannot be region-read.

    The step after CONVERT_IMAGE does not call read_info: preprocessing, tiled
    stitching, registration and qc.py all go through `tiled_io.open_lazy` --
    tifffile's zarr view -- and then `read_decimated` off that view. A tile
    layout or a compression the zarr view cannot serve produces a green
    read_info and a dead pipeline one process later, which is precisely the
    seam a format matrix exists to cover.
    """
    code, outdir = _convert(monkeypatch, tmp_path, fixture, channels)
    assert code == 0
    out = outdir / basename

    view, dtype, close = tiled_io.open_lazy(str(out))
    try:
        assert view.shape[0] == channel_count
        assert view.shape[1:] == ome_io.read_info(out).shape_cyx[1:]
        # A REGION read, not the whole plane: the property the streaming
        # gigapixel stitch relies on, and the one a bad tile layout breaks.
        corner = np.asarray(view[channel_count - 1, 0:8, 0:8])
        assert corner.shape == (8, 8)
        assert corner.dtype == dtype

        for index in range(channel_count):
            decimated = tiled_io.read_decimated(view, index, factor=1)
            assert decimated.shape == view.shape[1:]
            # read_decimated's contract is float32 unconditionally, for all three
            # of its callers -- see its docstring.
            assert decimated.dtype == np.dtype("float32")
    finally:
        close()


def test_the_channels_file_matches_the_output_channel_order(monkeypatch, tmp_path):
    """`<patient>_channels.txt` is how CONVERT_IMAGE propagates the channel order
    into meta.channels. If it disagreed with the OME header, every downstream
    per-channel read would be off by the reorder."""
    code, outdir = _convert(
        monkeypatch, tmp_path, "tests/testdata/fmt_pyramid.ome.tiff"
    )
    assert code == 0
    written = (outdir / "P001_channels.txt").read_text().strip().split(",")
    info = ome_io.read_info(outdir / "P001_DAPI_CD3.ome.tif")
    assert written == list(info.channels) == ["DAPI", "CD3"]


def test_the_nuclear_channel_is_moved_to_position_zero(monkeypatch, tmp_path):
    """The invariant every downstream step trusts: channel 0 is the nuclear
    marker. Declared here as PANCK|DAPI|SMA against the 3-channel INTERLEAVED RGB
    fixture, so the reorder is a real permutation of real pixels across the very
    axis remap that read_plane had to learn -- not a metadata edit."""
    code, outdir = _convert(
        monkeypatch,
        tmp_path,
        "tests/testdata/fmt_rgb.tiff",
        channels="PANCK,DAPI,SMA",
    )
    assert code == 0
    out = outdir / "P001_DAPI_PANCK_SMA.ome.tif"
    assert out.exists(), (
        "the output is named after the OUTPUT channel order, so "
        f"{sorted(p.name for p in outdir.iterdir())} means the nuclear channel never moved"
    )
    info = ome_io.read_info(out)
    assert list(info.channels)[0] == "DAPI"

    # The PIXELS moved with the name. Plane 0 of the output must equal plane 1 of
    # the input (the declared DAPI) and NOT plane 0 -- the second half matters,
    # because on a fixture whose samples happened to be equal the first would pass
    # against an unmoved image.
    source = _planes_of(_fixture("tests/testdata/fmt_rgb.tiff"), 3)
    written = _planes_of(out, 3)
    assert np.array_equal(written[0], source[1])
    assert not np.array_equal(written[0], source[0])
    # ...and the two displaced channels kept their own pixels rather than being
    # rolled: PANCK was input 0, SMA was input 2.
    assert np.array_equal(written[1], source[0])
    assert np.array_equal(written[2], source[2])


def test_an_interleaved_rgb_source_becomes_three_planar_channels(monkeypatch, tmp_path):
    """fmt_rgb.tiff is stored YXS: three colour SAMPLES of one channel, which
    bioio reports as C=1/S=3 and whose only channel name is 'Channel:0:0'. The
    conversion's job is to turn that into three real, separately named, planar
    channels -- and it is the reason no open_lazy consumer ever has to know what
    an interleaved file is."""
    code, outdir = _convert(
        monkeypatch,
        tmp_path,
        "tests/testdata/fmt_rgb.tiff",
        channels="DAPI,PANCK,SMA",
    )
    assert code == 0
    info = ome_io.read_info(outdir / "P001_DAPI_PANCK_SMA.ome.tif")
    assert info.shape_cyx == (3, 64, 64)
    assert list(info.channels) == ["DAPI", "PANCK", "SMA"]
    assert info.channels_are_samples is False

    # Same three sample planes, same values, now on the channel axis.
    source = _planes_of(_fixture("tests/testdata/fmt_rgb.tiff"), 3)
    written = _planes_of(outdir / "P001_DAPI_PANCK_SMA.ome.tif", 3)
    for expected, actual in zip(source, written):
        assert np.array_equal(actual, expected)


def test_a_truncated_slide_fails_with_an_exit_code_and_writes_nothing(
    monkeypatch, tmp_path
):
    """The dangerous shape is a PARTIAL output: a half-written OME-TIFF is
    indistinguishable from a real one four processes later. Also pins that the
    failure is an EXIT CODE rather than a hang -- fmt_truncated.ome.tiff stops
    past the TIFF header and short of the first IFD, the shape a reader is most
    likely to sit on."""
    code, outdir = _convert(
        monkeypatch, tmp_path, "tests/testdata/fmt_truncated.ome.tiff"
    )
    assert code == 1
    assert list(outdir.glob("*.ome.tif")) == []
    # main() writes <patient>_channels.txt only AFTER a successful conversion.
    # A channels file with no slide beside it would let the next process start.
    assert list(outdir.glob("*_channels.txt")) == []


def test_an_unsupported_suffix_is_refused_before_any_reader_is_asked(
    monkeypatch, tmp_path
):
    """A .png reaching a reader used to fail with a bioio plugin-installation
    message that named neither the suffix nor what this pipeline supports.
    detect_reader raises UnsupportedFormatError from the suffix table instead,
    and main() turns that into exit 1 like any other conversion failure."""
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    outdir = tmp_path / "out"
    monkeypatch.setattr(
        "sys.argv",
        [
            "convert_image.py",
            "--input_file",
            str(png),
            "--output_dir",
            str(outdir),
            "--patient_id",
            "P001",
            "--pixel_size",
            "0.325",
            "--channels",
            "DAPI",
            "--nuclear-markers",
            *PIPELINE_NUCLEAR_MARKERS,
        ],
    )
    assert convert_image.main() == 1
    # The refusal happens before the read, so nothing is created at all --
    # including the output directory, which convert_to_ome_tiff makes late.
    assert not outdir.exists()

    # And the message names the suffix and the supported set, which is the whole
    # point of routing this through detect_reader rather than falling through.
    with pytest.raises(ome_io.UnsupportedFormatError) as exc:
        ome_io.detect_reader(png)
    assert ".png" in str(exc.value)
    assert ".ome.tiff" in str(exc.value)
