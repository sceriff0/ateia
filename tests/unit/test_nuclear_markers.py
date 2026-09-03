#!/usr/bin/env python3
"""Nuclear/fiducial channel resolution.

The nuclear channel is resolved by marker NAME from metadata (never the filename),
using an ordered preference list. These tests pin the ordering, case-insensitivity,
the no-match contract of the shared helper, CONVERT_IMAGE's use of it (reorder
to channel 0, fail-fast when absent, single-channel exception), and -- in
``TestSplitCountMatchesPrecomputedCount`` -- the cross-language agreement the
postprocessing group sizes depend on.
"""

from pathlib import Path

import numpy as np
import pytest
import tifffile
from split_multichannel import split_multichannel_tiff
from utils.metadata import DEFAULT_NUCLEAR_MARKERS, is_nuclear, pick_nuclear_index


class TestPickNuclearIndex:
    def test_default_markers_are_dapi_then_celltox(self):
        assert list(DEFAULT_NUCLEAR_MARKERS) == ["DAPI", "CELLTOX"]

    def test_dapi_wins_when_both_present(self):
        # Marker preference (DAPI first) beats channel order.
        assert pick_nuclear_index(["CELLTOX", "CD8", "DAPI"]) == 2

    def test_celltox_used_when_dapi_absent(self):
        assert pick_nuclear_index(["CD8", "CELLTOX", "CD68"]) == 1

    def test_returns_none_when_no_marker_matches(self):
        assert pick_nuclear_index(["CD8", "CD68", "PDL1"]) is None

    def test_case_insensitive_substring_match(self):
        assert pick_nuclear_index(["cd8", "dapi-nuclear"]) == 1

    def test_marker_order_is_honored_over_channel_order(self):
        assert pick_nuclear_index(["DAPI", "CELLTOX"], ["CELLTOX", "DAPI"]) == 1

    def test_empty_or_none_channel_list_returns_none(self):
        assert pick_nuclear_index([]) is None
        assert pick_nuclear_index(None) is None


class TestIsNuclear:
    """The per-channel predicate, shared with lib/MarkerUtils.groovy's isNuclear.

    Same rule, two languages: case-insensitive SUBSTRING. Anything asserted here has
    a counterpart in MarkerUtils, and the two must be changed together.
    """

    def test_exact_marker_matches(self):
        assert is_nuclear("DAPI", ["DAPI"])

    def test_substring_match_so_convert_image_stays_in_agreement(self):
        # CONVERT_IMAGE already moved 'DAPI_nuclear' to channel 0 via
        # pick_nuclear_index's substring rule; an exact match here would then keep it
        # on non-reference slides and over-fill the patient's group.
        assert is_nuclear("DAPI_nuclear", ["DAPI"])

    def test_case_insensitive(self):
        assert is_nuclear("celltox-fiducial", ["CELLTOX"])

    def test_non_marker_channel_is_not_nuclear(self):
        assert not is_nuclear("PANCK", ["DAPI", "CELLTOX"])

    def test_celltox_is_not_nuclear_when_only_dapi_is_configured(self):
        assert not is_nuclear("CELLTOX", ["DAPI"])

    def test_blank_channel_name_is_not_nuclear(self):
        assert not is_nuclear("", ["DAPI"])
        assert not is_nuclear(None, ["DAPI"])

    def test_a_comma_joined_element_is_split_not_treated_as_one_marker(self):
        # The shape a params file produces when the list is written as one string:
        # ["DAPI,CELLTOX"]. Unsplit it is a single marker that matches NO channel, so
        # every channel is silently classified non-nuclear and the nuclear channel is
        # never dropped from moving slides. lib/MarkerUtils.groovy's markerList splits
        # the same way; the two must agree.
        assert is_nuclear("DAPI", ["DAPI,CELLTOX"])
        assert is_nuclear("CELLTOX", ["DAPI,CELLTOX"])
        assert not is_nuclear("PANCK", ["DAPI,CELLTOX"])

    def test_a_space_joined_element_is_split_too(self):
        assert is_nuclear("CELLTOX", ["DAPI CELLTOX"])

    def test_falls_back_to_the_one_permitted_mirror(self):
        # bin/utils/metadata.py holds the ONLY Python mirror of nextflow.config's
        # default; MarkerUtils deliberately has none and throws instead.
        assert is_nuclear("CELLTOX")
        assert is_nuclear("DAPI")


def _write_multichannel_tiff(path, n_channels):
    """A tiny (C, Y, X) uint16 stack -- content is irrelevant, channel count is not."""
    data = np.zeros((n_channels, 4, 4), dtype=np.uint16)
    for c in range(n_channels):
        data[c] = c + 1
    tifffile.imwrite(str(path), data)
    return path


class TestSplitCountMatchesPrecomputedCount:
    """The invariant the whole nuclear-marker rule rests on.

    Nextflow sizes each patient's postprocessing ``groupTuple`` AHEAD of the run, from
    ``CsvUtils.countChannelsPerPatient`` -> ``CsvUtils.resolveKeptChannelsPerSlide``.
    The files that actually arrive come from ``bin/split_multichannel.py``. If the two
    disagree the failure is silent, not loud:

      * Python emits MORE than Groovy counted -> the group over-fills, ``remainder:
        true`` emits the surplus as a SECOND group for the same patient, and
        ``MERGE_QUANT_CSVS`` runs twice against one
        ``<outdir>/<pid>/quantification/merged_quant.csv``. On the linear path
        ``postprocess.nf``'s ``join(ch_morphology, by: 0)`` instead discards the
        surplus, so ``merged_quant.csv`` quietly loses markers.
      * Python emits FEWER -> the group never fills.

    The cases below exercise the FALLBACK path -- ``split_multichannel_tiff`` with no
    ``keep_channels`` -- which is what ``SPLIT_PRIOR_PYRAMID`` uses, since it reads
    channel names from OME-XML at runtime and cannot be handed a precomputed list.
    The primary path is covered by ``TestKeepChannels`` below.

    The Groovy half of the same expectation is asserted in ``tests/main.nf.test``,
    which runs the stub pipeline over ``tests/testdata/celltox_nonreference.csv`` /
    ``celltox_only.csv``. Note the two sheets now diverge: ``celltox_nonreference``
    yields SIX markers (the reference never carried CELLTOX, so the moving slide keeps
    it), while ``celltox_only`` still yields five (the reference DOES carry CELLTOX, so
    the moving slide's copy is redundant and is claimed away). pytest cannot execute
    Groovy, so the two halves are pinned against a shared literal rather than against
    each other; change one and you must change the other.
    """

    @pytest.mark.parametrize(
        "channels,is_reference,markers,expected",
        [
            # tests/testdata/celltox_nonreference.csv, on the SHIPPED DEFAULT marker
            # list. Groovy counts {DAPI,PANCK,SMA} + {CD3,CD8} = 5 for the patient.
            (["DAPI", "PANCK", "SMA"], True, ["DAPI", "CELLTOX"], 3),
            (["CELLTOX", "CD3", "CD8"], False, ["DAPI", "CELLTOX"], 2),
            # tests/testdata/celltox_only.csv, under --nuclear_markers CELLTOX.
            # Groovy counts {CELLTOX,PANCK,SMA} + {CD3,CD8} = 5.
            (["CELLTOX", "PANCK", "SMA"], True, ["CELLTOX"], 3),
            (["CELLTOX", "CD3", "CD8"], False, ["CELLTOX"], 2),
            # The regression the hardcoded `"DAPI" in name.upper()` caused: with only
            # CELLTOX configured, DAPI is an ordinary marker and is NOT dropped.
            (["DAPI", "CD3", "CD8"], False, ["CELLTOX"], 3),
        ],
    )
    def test_emitted_channel_count(
        self, tmp_path, channels, is_reference, markers, expected
    ):
        src = _write_multichannel_tiff(tmp_path / "in.tiff", len(channels))
        out = tmp_path / f"out_{'ref' if is_reference else 'mov'}_{len(markers)}"
        saved = split_multichannel_tiff(
            str(src), str(out), is_reference, list(channels), markers, pixel_size=0.325
        )
        assert len(saved) == expected

    def test_the_dropped_channel_is_the_nuclear_one(self, tmp_path):
        src = _write_multichannel_tiff(tmp_path / "in.tiff", 3)
        saved = split_multichannel_tiff(
            str(src),
            str(tmp_path / "out"),
            False,
            ["CELLTOX", "CD3", "CD8"],
            ["DAPI", "CELLTOX"],
            pixel_size=0.325,
        )
        assert sorted(Path(p).name for p in saved) == ["CD3.tiff", "CD8.tiff"]

    def test_substring_named_nuclear_channel_is_dropped_too(self, tmp_path):
        # CONVERT_IMAGE resolves 'CELLTOX_fiducial' as nuclear (substring rule) and
        # MarkerUtils counts it as dropped, so the split must drop it as well.
        src = _write_multichannel_tiff(tmp_path / "in.tiff", 2)
        saved = split_multichannel_tiff(
            str(src),
            str(tmp_path / "out"),
            False,
            ["CELLTOX_fiducial", "CD3"],
            ["DAPI", "CELLTOX"],
            pixel_size=0.325,
        )
        assert [Path(p).name for p in saved] == ["CD3.tiff"]

    def test_reference_keeps_every_channel(self, tmp_path):
        src = _write_multichannel_tiff(tmp_path / "in.tiff", 3)
        saved = split_multichannel_tiff(
            str(src),
            str(tmp_path / "out"),
            True,
            ["CELLTOX", "CD3", "CD8"],
            ["CELLTOX"],
            pixel_size=0.325,
        )
        assert sorted(Path(p).name for p in saved) == [
            "CD3.tiff",
            "CD8.tiff",
            "CELLTOX.tiff",
        ]


def _fake_read_image(channel_count):
    """Return a (read_image-compatible) stand-in producing a (C, Y, X) uint16 image."""

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


class TestConvertImageNuclearResolution:
    def test_dapi_moved_to_channel_zero(self, tmp_path, monkeypatch):
        import convert_image

        monkeypatch.setattr(convert_image, "read_image", _fake_read_image(3))
        _out, output_channels = convert_image.convert_to_ome_tiff(
            Path("in.ome.tif"),
            tmp_path,
            "P001",
            ["CD8", "DAPI", "CD68"],
            pixel_size_um=0.325,
        )
        assert output_channels[0] == "DAPI"
        assert set(output_channels) == {"CD8", "DAPI", "CD68"}

    def test_celltox_used_when_dapi_absent(self, tmp_path, monkeypatch):
        import convert_image

        monkeypatch.setattr(convert_image, "read_image", _fake_read_image(3))
        _out, output_channels = convert_image.convert_to_ome_tiff(
            Path("in.ome.tif"),
            tmp_path,
            "P001",
            ["CD8", "CELLTOX", "CD68"],
            pixel_size_um=0.325,
        )
        assert output_channels[0] == "CELLTOX"

    def test_fail_fast_when_no_nuclear_marker_multichannel(self, tmp_path, monkeypatch):
        import convert_image

        monkeypatch.setattr(convert_image, "read_image", _fake_read_image(2))
        with pytest.raises(ValueError, match="nuclear_markers"):
            convert_image.convert_to_ome_tiff(
                Path("in.ome.tif"),
                tmp_path,
                "P001",
                ["CD8", "CD68"],
                pixel_size_um=0.325,
            )

    def test_single_channel_assumed_nuclear(self, tmp_path, monkeypatch):
        import convert_image

        monkeypatch.setattr(convert_image, "read_image", _fake_read_image(1))
        _out, output_channels = convert_image.convert_to_ome_tiff(
            Path("in.ome.tif"), tmp_path, "P001", ["CD8"], pixel_size_um=0.325
        )
        assert output_channels == ["CD8"]

    def test_custom_nuclear_markers_override(self, tmp_path, monkeypatch):
        import convert_image

        monkeypatch.setattr(convert_image, "read_image", _fake_read_image(2))
        _out, output_channels = convert_image.convert_to_ome_tiff(
            Path("in.ome.tif"),
            tmp_path,
            "P001",
            ["CD8", "SMA"],
            nuclear_markers=["SMA"],
            pixel_size_um=0.325,
        )
        assert output_channels[0] == "SMA"


class TestKeepChannels:
    """The explicit keep-set, which supersedes the is_reference nuclear drop.

    ``CsvUtils.resolveKeptChannelsPerSlide`` decides once per slide, at samplesheet
    read, which channels that slide emits, and the list travels to this script as
    ``--keep-channels``. When it is supplied it IS the decision: ``is_reference`` and
    ``nuclear_markers`` are not consulted. That is what lets a patient whose reference
    stains DAPI and whose later cycle stains CELLTOX keep BOTH markers, instead of
    losing CELLTOX merely for matching the configured nuclear list.

    The fallback (no keep-set) must stay intact: ``SPLIT_PRIOR_PYRAMID`` reads channel
    names from OME-XML at runtime and so cannot be handed a precomputed list.
    """

    @staticmethod
    def _write(tmp_path, n_channels):
        img = np.zeros((n_channels, 8, 8), dtype=np.uint16)
        src = tmp_path / "slide.ome.tiff"
        tifffile.imwrite(str(src), img)
        out = tmp_path / "out"
        out.mkdir()
        return src, out

    def test_keep_set_wins_over_the_nuclear_drop(self, tmp_path):
        """The reported bug: a non-reference CELLTOX slide keeps CELLTOX."""
        src, out = self._write(tmp_path, 2)
        split_multichannel_tiff(
            str(src),
            str(out),
            is_reference=False,
            channel_names=["CELLTOX", "CD8"],
            nuclear_markers=["DAPI", "CELLTOX"],
            keep_channels=["CELLTOX", "CD8"],
            pixel_size=0.325,
        )
        assert sorted(p.stem for p in out.glob("*.tiff")) == ["CD8", "CELLTOX"]

    def test_keep_set_can_drop_a_non_nuclear_marker(self, tmp_path):
        """A marker already claimed by an earlier slide is dropped, nuclear or not."""
        src, out = self._write(tmp_path, 2)
        split_multichannel_tiff(
            str(src),
            str(out),
            is_reference=False,
            channel_names=["KI67", "FOXP3"],
            nuclear_markers=["DAPI", "CELLTOX"],
            keep_channels=["FOXP3"],
            pixel_size=0.325,
        )
        assert sorted(p.stem for p in out.glob("*.tiff")) == ["FOXP3"]

    def test_keep_set_matching_is_case_insensitive(self, tmp_path):
        src, out = self._write(tmp_path, 1)
        split_multichannel_tiff(
            str(src),
            str(out),
            is_reference=False,
            channel_names=["CellTox"],
            nuclear_markers=["DAPI", "CELLTOX"],
            keep_channels=["celltox"],
            pixel_size=0.325,
        )
        assert [p.stem for p in out.glob("*.tiff")] == ["CellTox"]

    def test_no_keep_set_falls_back_to_the_nuclear_rule(self, tmp_path):
        """SPLIT_PRIOR_PYRAMID's path: no list, so is_reference decides as before."""
        src, out = self._write(tmp_path, 2)
        split_multichannel_tiff(
            str(src),
            str(out),
            is_reference=False,
            channel_names=["CELLTOX", "CD8"],
            nuclear_markers=["DAPI", "CELLTOX"],
            pixel_size=0.325,
        )
        assert sorted(p.stem for p in out.glob("*.tiff")) == ["CD8"]
