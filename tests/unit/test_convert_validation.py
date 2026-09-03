"""What CONVERT_IMAGE REFUSES, which had no coverage at all.

`raise ValueError("Channel count mismatch: ...")` in bin/convert_image.py's
convert_to_ome_tiff was never exercised: every existing caller
(tests/unit/test_nuclear_markers.py) passes a channel list whose length already
matches the fake reader's channel count. That branch is the one thing standing
between a mistyped samplesheet `channels` cell and a slide whose channel names
are silently attached to the wrong planes for the rest of the run -- a wrong
number, not a crash, in every downstream measurement.

THE READER IS FAKED HERE, DELIBERATELY, so this file runs in the MAIN suite with
no bioio and no h5py installed (requirements/ci.txt installs neither). The
real-bytes conversions live in tests/integration/formats/, behind the convert
image's own reader stack, and run only in the `format-tests` job.

That split is why the truncation case below goes through `ome_io.read_info`
rather than `convert_image.main()`. In THIS environment a truncated .ome.tiff
would make main() return 1 for the wrong reason -- `require_reader("bioio")`
fails before the file is ever opened -- so the exit code would be a fake green.
read_info reaches the same bytes through tifffile, which IS installed
everywhere. The CLI-level version of that refusal is
tests/integration/formats/test_convert_image_end_to_end.py's
test_a_truncated_slide_fails_with_an_exit_code_and_writes_nothing.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest

# `ome_io` is a bin/utils module importing its siblings flat, so both `bin` and
# `bin/utils` must be on sys.path -- matching tests/test_ome_io.py.
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "bin"))
sys.path.insert(0, str(_ROOT / "bin" / "utils"))

import ome_io  # noqa: E402


def _fake_read_image(channel_count):
    """A read_image stand-in producing a (C, Y, X) uint16 image."""

    def _reader(_path):
        img = np.zeros((channel_count, 4, 4), dtype=np.uint16)
        for c in range(channel_count):
            img[c] = c + 1
        meta = {
            "original_dims": "CYX",
            "num_channels": channel_count,
            "channel_names_from_file": None,
        }
        return img, meta

    return _reader


def _argv(**overrides):
    """CONVERT_IMAGE's rendered command line, flag for flag.

    modules/local/convert_image.nf always passes --channels and
    --nuclear-markers, so the argv shape exercised here is the one the pipeline
    actually renders rather than whatever argparse would default to.
    """
    args = {
        "--input_file": None,
        "--output_dir": None,
        "--patient_id": "P001",
        "--pixel_size": "0.325",
        "--channels": "DAPI",
    }
    args.update(overrides)
    argv = ["convert_image.py"]
    for flag, value in args.items():
        argv += [flag, str(value)]
    argv += ["--nuclear-markers", "DAPI", "CELLTOX"]
    return argv


# ---------------------------------------------------------------------------
# The channel-count contract
# ---------------------------------------------------------------------------


def test_declaring_too_few_channels_is_refused_with_both_numbers(tmp_path, monkeypatch):
    import convert_image

    monkeypatch.setattr(convert_image, "read_image", _fake_read_image(3))
    with pytest.raises(ValueError) as exc:
        convert_image.convert_to_ome_tiff(
            Path("in.ome.tif"), tmp_path, "P001", ["DAPI", "CD3"], pixel_size_um=0.325
        )
    message = str(exc.value)
    assert "Channel count mismatch" in message
    assert "3" in message and "2" in message, (
        "the error must name BOTH counts -- an operator fixing a samplesheet needs "
        f"to know which side is wrong: {message!r}"
    )


def test_declaring_too_many_channels_is_refused(tmp_path, monkeypatch):
    import convert_image

    monkeypatch.setattr(convert_image, "read_image", _fake_read_image(2))
    with pytest.raises(ValueError, match="Channel count mismatch"):
        convert_image.convert_to_ome_tiff(
            Path("in.ome.tif"),
            tmp_path,
            "P001",
            ["DAPI", "CD3", "CD8"],
            pixel_size_um=0.325,
        )


def test_a_mismatch_writes_no_output_at_all(tmp_path, monkeypatch):
    """The validation runs BEFORE the write. A half-written slide left behind by
    a rejected conversion is worse than the rejection."""
    import convert_image

    monkeypatch.setattr(convert_image, "read_image", _fake_read_image(3))
    # `match=` is not decoration: with the count check removed, write_ome_tiff's
    # OWN channels-vs-data guard raises a ValueError further down and a bare
    # pytest.raises(ValueError) here goes green on it -- measured. The point of
    # this test is that CONVERT_IMAGE refuses BEFORE the write, so the message
    # has to be the one from before the write.
    with pytest.raises(ValueError, match="Channel count mismatch"):
        convert_image.convert_to_ome_tiff(
            Path("in.ome.tif"), tmp_path, "P001", ["DAPI"], pixel_size_um=0.325
        )
    assert list(tmp_path.glob("*.ome.tif")) == []


def test_a_file_with_no_channel_names_and_no_declaration_is_refused(
    tmp_path, monkeypatch
):
    """`channel_names_from_file` is None for a plain TIFF. Continuing from there
    would mean inventing names, which is how a marker column ends up labelled
    after a channel it does not contain."""
    import convert_image

    monkeypatch.setattr(convert_image, "read_image", _fake_read_image(2))
    with pytest.raises(ValueError, match="No channel names"):
        convert_image.convert_to_ome_tiff(
            Path("in.ome.tif"), tmp_path, "P001", None, pixel_size_um=0.325
        )


# ---------------------------------------------------------------------------
# main()'s exit codes
# ---------------------------------------------------------------------------


def test_main_returns_one_for_an_input_that_does_not_exist(
    tmp_path, monkeypatch, caplog
):
    """The exit code alone does NOT pin this, and measuring that was the point:
    with main()'s existence check deleted, an absent .ome.tiff still exits 1 --
    detect_reader routes it to bioio, require_reader finds bioio absent in the
    main suite's environment, and the ImportError becomes exit 1 several frames
    later. Green, for a reason that has nothing to do with the missing file, and
    with an operator-facing message naming the wrong problem. So the MESSAGE is
    what is asserted."""
    import convert_image

    absent = tmp_path / "absent.ome.tiff"
    monkeypatch.setattr(
        "sys.argv",
        _argv(**{"--input_file": absent, "--output_dir": tmp_path / "out"}),
    )
    with caplog.at_level(logging.ERROR):
        assert convert_image.main() == 1
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("Input file not found" in m and str(absent) in m for m in errors), (
        f"the failure must name the missing path, not a reader: {errors!r}"
    )


def test_main_turns_a_conversion_failure_into_exit_one_not_a_traceback(
    tmp_path, monkeypatch
):
    """A traceback out of a Nextflow task is an exit code too -- but a caught
    failure is the one that gets the operator a readable log line. This pins that
    main() catches rather than propagates, which is what makes CONVERT_IMAGE's
    retry policy meaningful."""
    import convert_image

    source = tmp_path / "in.ome.tiff"
    source.write_bytes(b"not a tiff")

    def _explode(*_args, **_kwargs):
        raise RuntimeError("reader said no")

    monkeypatch.setattr(convert_image, "convert_to_ome_tiff", _explode)
    monkeypatch.setattr(
        "sys.argv",
        _argv(**{"--input_file": source, "--output_dir": tmp_path / "out"}),
    )
    assert convert_image.main() == 1


def test_main_writes_no_channels_file_when_the_conversion_fails(tmp_path, monkeypatch):
    """`<patient>_channels.txt` is what propagates the channel order into
    meta.channels. main() writes it AFTER the conversion, so a failed run leaves
    none -- if it did, the next process would start on a slide that is not there."""
    import convert_image

    source = tmp_path / "in.ome.tiff"
    source.write_bytes(b"not a tiff")
    outdir = tmp_path / "out"
    outdir.mkdir()

    def _explode(*_args, **_kwargs):
        raise RuntimeError("reader said no")

    monkeypatch.setattr(convert_image, "convert_to_ome_tiff", _explode)
    monkeypatch.setattr(
        "sys.argv", _argv(**{"--input_file": source, "--output_dir": outdir})
    )
    assert convert_image.main() == 1
    assert list(outdir.iterdir()) == []


# ---------------------------------------------------------------------------
# The three refusals that happen before, or instead of, a read
# ---------------------------------------------------------------------------


def test_an_unsupported_suffix_is_refused_and_the_message_names_it(
    tmp_path, monkeypatch
):
    """A .png in a samplesheet used to reach BioImage and fail several frames
    inside a plugin, naming a problem that was not the problem. convert_image's
    read_image now asks detect_reader first, and main() turns that into exit 1.

    Asserted at the CONVERT_IMAGE level on purpose: tests/test_ome_io.py already
    pins detect_reader's own message, and what was missing is that the converter
    routes through it at all rather than round-tripping to a reader.
    """
    import convert_image

    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    with pytest.raises(ome_io.UnsupportedFormatError) as exc:
        convert_image.read_image(png)
    message = str(exc.value)
    assert ".png" in message, f"the message must name the suffix: {message!r}"
    assert ".ome.tiff" in message, (
        f"...and list what IS supported, so the operator can act: {message!r}"
    )

    outdir = tmp_path / "out"
    monkeypatch.setattr(
        "sys.argv", _argv(**{"--input_file": png, "--output_dir": outdir})
    )
    assert convert_image.main() == 1
    # The refusal precedes the read, so nothing is created -- convert_to_ome_tiff
    # is what makes the output directory, and it is never reached.
    assert not outdir.exists()


def test_a_missing_reader_names_the_distribution_rather_than_a_bare_import_error(
    monkeypatch,
):
    """require_reader is the difference between "install bioio" and a
    ModuleNotFoundError several frames inside a plugin.

    `importlib.util.find_spec` is monkeypatched to report EVERYTHING absent, so
    this asserts unconditionally instead of skipping wherever bioio happens to be
    installed -- which is exactly what the format-tests job is. A refusal that is
    only checked in the environments that cannot trigger it is not checked.
    """
    # Imported BEFORE the patch: `importlib.util.find_spec` is not what the import
    # system itself uses, but leaving a first-time import of the module under test
    # inside a patched-import-machinery window is an ordering dependency waiting to
    # happen.
    import convert_image

    monkeypatch.setattr(importlib.util, "find_spec", lambda *_a, **_k: None)

    with pytest.raises(ImportError) as exc:
        ome_io.require_reader("bioio")
    message = str(exc.value)
    assert "bioio" in message
    assert "containers/convert" in message, (
        "the message must say WHICH image carries the reader, not just that one is "
        f"missing: {message!r}"
    )

    # And the converter reaches it: read_image calls require_reader before it
    # constructs anything, so an .ome.tif in a reader-less environment fails here
    # rather than inside BioImage.
    with pytest.raises(ImportError):
        convert_image.read_image(Path("slide.ome.tif"))


def test_a_truncated_slide_fails_at_the_read_rather_than_hanging_or_returning_zeros():
    """tests/testdata/fmt_truncated.ome.tiff is 100 bytes: past the TIFF header,
    short of the first IFD. Two failure shapes matter and neither is an
    exception. A reader that returns an EMPTY array hands the pipeline an
    all-zero slide that survives four more processes; a reader that blocks
    looking for the IFD burns the task's whole walltime and fails as a timeout,
    which reads as a cluster problem rather than a corrupt input.

    The exception's WORDING is not asserted -- it is tifffile's ("corrupted IFD
    structure"), not this pipeline's, and pinning it would make a tifffile bump a
    test failure. The type and the promptness are what this repo owns.
    """
    truncated = _ROOT / "tests/testdata/fmt_truncated.ome.tiff"
    assert truncated.exists(), (
        f"{truncated} is missing. Run "
        "`python tests/testdata/generate_complete_testdata.py` -- tests/testdata/ "
        "is gitignored, so every fixture exists only because the generator wrote it."
    )
    assert os.path.getsize(truncated) > 0, (
        "a ZERO-byte file would make this test pass for the wrong reason: it is "
        "the non-empty-but-unopenable shape that is under assertion."
    )

    started = time.monotonic()
    with pytest.raises(Exception):
        ome_io.read_info(truncated)
    elapsed = time.monotonic() - started
    assert elapsed < 10.0, (
        f"read_info took {elapsed:.1f}s on a 100-byte file -- that is the hang, not "
        "the refusal."
    )
