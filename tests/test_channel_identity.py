"""A marker's identity must not travel through a filename.

The defect this file pins, in one line: a channel declared `HLA.DR` in the
samplesheet was written to disk as `HLA_DR.tiff` (a '.' is outside the filename
allowlist), and the published measurement key was then rebuilt from the
SANITISED STEM -- so FlowPath saw `HLA_DR: Cell: Median` for a panel that
declared `HLA.DR`.

Two forms, one owner:

- the DECLARED name is what the samplesheet says and what fills the `<marker>`
  slot of the `"<marker>: <Compartment>: <Statistic>"` key (G5 contract with
  qupath-extension-flowpath);
- the FILE STEM is the sanitised, filesystem-safe form, and is used for
  filenames and nothing else.

`bin/utils/channel_name.py` owns the declared -> stem mapping on the Python
side; `lib/ChannelName.groovy` owns it on the Groovy/Nextflow side and is the
one the pipeline actually calls (SPLIT_CHANNELS passes the stems it computed
into `bin/split_multichannel.py` with `--file-stems`, so the `script:` and
`stub:` paths cannot name the same channel differently). The Python copy is the
standalone / OME-metadata fallback. `SANITISER_TABLE` below is the shared
table both implementations are held to; the Groovy half of it is asserted in
`tests/lib_probe.nf` (pytest cannot load a lib/ class, and nf-test's assertion
shell has no lib/ on its classpath -- see that file's header).
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / "bin"
sys.path.insert(0, str(BIN / "utils"))

from channel_name import file_stem, file_stems  # noqa: E402
from measurements import measurement_key  # noqa: E402


def _load_bin_module(name: str):
    """Load a bin/*.py script as a module (it puts bin/utils on sys.path itself)."""
    spec = importlib.util.spec_from_file_location(name, BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# The one table both language implementations are held to. Anything added here
# must be added to lib/ChannelName.groovy's assertions in tests/lib_probe.nf.
SANITISER_TABLE = [
    ("DAPI", "DAPI"),
    ("HLA.DR", "HLA_DR"),
    ("CD3-105", "CD3-105"),
    ("CD8_beta", "CD8_beta"),
    ("Ki-67", "Ki-67"),
    ("CD3 alpha", "CD3_alpha"),
    ("pS6(240/244)", "pS6_240_244_"),
    ("β-catenin", "_-catenin"),
]


# ── the sanitiser ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("declared,stem", SANITISER_TABLE)
def test_file_stem_keeps_only_the_filename_allowlist(declared, stem):
    """`[A-Za-z0-9-_]` survives; everything else becomes '_'.

    The allowlist is ASCII on purpose: a stem is a filename, and the two
    implementations (Python here, Groovy in lib/ChannelName.groovy) can only be
    held to one rule if that rule does not depend on a Unicode table.
    """
    assert file_stem(declared) == stem


def test_file_stems_disambiguates_a_collision_deterministically():
    """Two declared names can sanitise to one stem; the stems must stay unique.

    Numbering is by POSITION IN THE DECLARED LIST, not by what is already on
    disk. `os.path.exists` numbering depended on which channels were actually
    written, which made the answer differ between a reference slide (nuclear
    channel kept) and a moving slide (nuclear channel dropped) -- and between
    the real split and the stub, which writes a different set of files.
    """
    assert file_stems(["CD3.105", "CD3-105", "CD3_105"]) == [
        "CD3_105",
        "CD3-105",
        "CD3_105_2",
    ]


def test_file_stems_is_independent_of_which_channels_are_written():
    """The stem for a given declared name does not move when a sibling is dropped."""
    full = file_stems(["DAPI", "CD3.105", "CD3_105"])
    assert full == ["DAPI", "CD3_105", "CD3_105_2"]
    # Same declared list, same answer, regardless of the reference flag downstream.
    assert file_stems(["DAPI", "CD3.105", "CD3_105"]) == full


def test_file_stems_is_index_aligned_with_its_input():
    names = [d for d, _ in SANITISER_TABLE]
    assert len(file_stems(names)) == len(names)


# ── the split writes the stems it is handed ───────────────────────────────────
def _three_channel_tiff(tmp_path):
    import tifffile

    data = np.random.randint(0, 500, size=(3, 16, 16), dtype=np.uint16)
    path = tmp_path / "in.tiff"
    tifffile.imwrite(path, data)
    return path


def test_split_multichannel_uses_the_stems_it_is_given(tmp_path):
    """SPLIT_CHANNELS computes the stems in Groovy and passes them down.

    That is what makes the `script:` and `stub:` paths agree: both read the same
    `ChannelName.fileStems` answer, rather than one sanitising in Python and the
    other not sanitising at all.
    """
    split = _load_bin_module("split_multichannel")
    out = tmp_path / "out"
    saved = split.split_multichannel_tiff(
        str(_three_channel_tiff(tmp_path)),
        str(out),
        is_reference=True,
        channel_names=["DAPI", "HLA.DR", "CD3 alpha"],
        nuclear_markers=["DAPI"],
        file_stems=["DAPI", "HLA_DR", "CD3_alpha"],
    )
    assert sorted(Path(p).name for p in saved) == [
        "CD3_alpha.tiff",
        "DAPI.tiff",
        "HLA_DR.tiff",
    ]


def test_split_multichannel_falls_back_to_its_own_sanitiser(tmp_path):
    """No `--file-stems` (standalone use, or names read from OME metadata)."""
    split = _load_bin_module("split_multichannel")
    out = tmp_path / "out"
    saved = split.split_multichannel_tiff(
        str(_three_channel_tiff(tmp_path)),
        str(out),
        is_reference=True,
        channel_names=["DAPI", "HLA.DR", "CD3 alpha"],
        nuclear_markers=["DAPI"],
    )
    assert sorted(Path(p).name for p in saved) == [
        "CD3_alpha.tiff",
        "DAPI.tiff",
        "HLA_DR.tiff",
    ]


def test_split_multichannel_ignores_misaligned_stems(tmp_path):
    """A stem list that does not line up with the channel list is not trusted.

    The channel list can be padded or truncated to match the image's real
    channel count; the stems would then be off by one, which would silently
    write a marker's pixels under another marker's name.
    """
    split = _load_bin_module("split_multichannel")
    out = tmp_path / "out"
    saved = split.split_multichannel_tiff(
        str(_three_channel_tiff(tmp_path)),
        str(out),
        is_reference=True,
        channel_names=["DAPI", "HLA.DR", "CD3 alpha"],
        nuclear_markers=["DAPI"],
        file_stems=["DAPI", "HLA_DR"],  # one short
    )
    assert sorted(Path(p).name for p in saved) == [
        "CD3_alpha.tiff",
        "DAPI.tiff",
        "HLA_DR.tiff",
    ]


# ── the contract change: key carries the DECLARED name, file the STEM ─────────
def test_declared_name_fills_the_key_while_the_stem_names_the_file(tmp_path):
    """THE central assertion of this task.

    `HLA.DR` on the samplesheet must produce the on-disk file `HLA_DR.tiff` AND
    the published key `HLA.DR: Cell: Median`. Before this change the key was
    rebuilt from the stem and read `HLA_DR: Cell: Median`.
    """
    declared = "HLA.DR"
    assert file_stem(declared) == "HLA_DR"

    quantify = _load_bin_module("quantify")
    cell_mask = np.array([[1, 1], [2, 2]], dtype=np.int32)
    channel = np.array([[10.0, 20.0], [30.0, 40.0]])
    df = quantify.compute_compartment_intensities(
        cell_mask, None, channel, declared, statistics=["Median"]
    )
    assert "HLA.DR: Cell: Median" in df.columns
    assert "HLA_DR: Cell: Median" not in df.columns


# ── the two producers of one measurement namespace ────────────────────────────
def test_phenotyping_reads_the_column_quantification_writes():
    """The reconciliation, as a behaviour rather than as a comment.

    `bin/phenotype_cells.py` builds its lookup key from the DECLARED panel name
    via `measurement_key`. `bin/quantify.py` writes the column. While quantify
    was fed the sanitised stem the two disagreed for every marker with a '.' in
    its name -- and `_marker_values`' miss is SILENT: it returns zeros and a
    `missing` flag, so the marker was gated on an all-zero column instead of
    failing.
    """
    quantify = _load_bin_module("quantify")
    pheno = _load_bin_module("phenotype_cells")

    declared = "HLA.DR"
    cell_mask = np.array([[1, 1], [2, 2]], dtype=np.int32)
    channel = np.array([[10.0, 20.0], [30.0, 40.0]])
    df = quantify.compute_compartment_intensities(
        cell_mask, None, channel, declared, statistics=["Median"]
    )

    values, missing, column = pheno._marker_values(
        df, declared, {"compartment": "Cell", "statistic": "Median"}
    )
    assert not missing, (
        "phenotyping fell back to its degraded all-zero path: the panel's "
        "declared marker name does not match the quantification column"
    )
    assert column == measurement_key(declared, "Cell", "Median")
    assert not np.allclose(values, 0.0)

    # The negative control: quantify fed the SANITISED STEM is exactly the state
    # this task removes, and it is silent.
    stem_df = quantify.compute_compartment_intensities(
        cell_mask, None, channel, file_stem(declared), statistics=["Median"]
    )
    _v, stem_missing, _c = pheno._marker_values(
        stem_df, declared, {"compartment": "Cell", "statistic": "Median"}
    )
    assert stem_missing


def test_quantify_builds_no_measurement_key_of_its_own():
    """Source-level: the grammar's separators appear in measurements.py only.

    `bin/quantify.py` used to spell `f"{channel_name}: {comp}: {name}"` twice --
    once in the producer and once again for the empty-result branch's header --
    which is three copies of a cross-repo contract in two files.
    """
    src = (BIN / "quantify.py").read_text()
    offenders = [
        line.strip()
        for line in src.splitlines()
        if re.search(r'f"\{[^"}]*\}: \{[^"}]*\}: \{', line)
    ]
    assert offenders == [], "quantify.py still builds the key itself: " + str(offenders)


def test_empty_result_header_comes_from_the_shared_builder(tmp_path, monkeypatch):
    """The empty-channel CSV header is the third copy the extraction missed.

    Redefining the shared builder must move BOTH the populated columns and the
    all-empty header; if the empty branch keeps its own f-string, only one moves
    and a channel with no cells silently ships a differently-spelled header.
    """
    import tifffile

    quantify = _load_bin_module("quantify")
    monkeypatch.setattr(
        quantify,
        "measurement_key",
        lambda marker, comp, stat: f"<{marker}|{comp}|{stat}>",
    )

    np.save(tmp_path / "mask.npy", np.zeros((4, 4), dtype=np.int32))
    tifffile.imwrite(tmp_path / "chan.tif", np.zeros((4, 4), dtype=np.uint16))
    out = tmp_path / "empty.csv"
    quantify.run_quantification(
        str(tmp_path / "mask.npy"),
        str(tmp_path / "chan.tif"),
        str(out),
        channel_name="HLA.DR",
        statistics=["Median"],
    )
    header = list(pd.read_csv(out).columns)
    assert header == ["label", "HLA.DR", "<HLA.DR|Cell|Median>"]
