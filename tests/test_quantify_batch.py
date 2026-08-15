"""One QUANTIFY task per patient — and the memory shape that makes it safe.

WHY THIS FILE EXISTS
--------------------
QUANTIFY used to run once per (patient x marker): 12 patients x 17 markers =
204 tasks, each asking for a flat 128 GB. Batching a patient's markers into ONE
task removes 17/18ths of that fan-out — but only if the batch is written in one
particular shape:

    load the masks ONCE; then, per channel: load -> compute -> DISCARD,
    before the next channel is loaded.

The naive shape — stack every channel into one array (or into a list) and then
loop over it — produces byte-identical numbers and would pass every equivalence
test in this repo, while multiplying peak resident memory by the marker count.
A 128 GB request becomes a genuine multi-terabyte one.

(An earlier revision of this docstring said that OOM could then be dropped by
`conf/modules.config`'s errorStrategy `'ignore'` branch. That was FALSE and is
corrected here rather than quietly deleted: the `'ignore'` branch at
`conf/modules.config:248` is scoped by the `withName:` at `:241` to seven QC
processes, and QUANTIFY is not one of them. QUANTIFY inherits
`conf/base.config`, where exit 137 retries up to `maxRetries = 3` and then
'finish'es — so the run FAILS. The naive shape is worth this much test because
it costs a patient's entire panel and a loud, expensive failure, not because it
hides.)

So the load-bearing test here is a MEMORY test, not a numbers test. It is
written against `bin/quantify.py`'s ONE channel-load seam, `_load_channel`, and
it asserts two independent things:

  1. INSTRUMENTED: at the moment channel k is loaded, no earlier channel plane
     is still alive. This catches "load them all, then loop" — the shape a
     reader reaches for first.
  2. MEASURED: tracemalloc's peak for N channels is not N times the peak for 1.
     This catches the shape instrumentation cannot see — copying each plane
     into a preallocated (N, Y, X) buffer and freeing the source, where the
     live-plane count stays 1 and memory still scales with N.

Both were watched failing against a deliberately-written naive implementation
before the real one was committed; the red output is quoted in the task report.

WHAT THIS FILE DOES NOT PIN
---------------------------
It does not pin the measurement itself. `compute_compartment_intensities` is
unchanged by the batching, and `tests/unit/test_quantify.py`,
`tests/test_quantify_median.py` and `tests/test_quantify_redsea.py` own that.
What is pinned here is that the ORCHESTRATION change is a no-op on the bytes:
the per-marker CSVs, and the merged table `bin/merge_quant_csvs.py` builds from
them, are byte-identical whether the markers were quantified one-task-each or
all in one task.
"""

from __future__ import annotations

import subprocess
import sys
import tracemalloc
import weakref
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import tifffile

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN / "utils"))
sys.path.insert(0, str(BIN))

import quantify  # noqa: E402
import redsea  # noqa: E402
import redsea_matrix  # noqa: E402

MARKERS = ["CD3", "PANCK", "SMA", "KI67", "CD20", "CD8"]


# ── fixtures ──────────────────────────────────────────────────────────────────
def _write_scene(tmp_path: Path, markers=MARKERS, shape=(48, 48)):
    """A patient's worth of inputs on disk: two masks and one tiff per marker.

    Deliberately tiny — the memory tests build their own, much larger, planes
    through a patched loader, so nothing here needs to be big.
    """
    ny, nx = shape
    cell = np.zeros(shape, dtype=np.int32)
    step = ny // 4
    for i in range(4):
        cell[i * step : (i + 1) * step, : nx // 2] = i + 1
        cell[i * step : (i + 1) * step, nx // 2 :] = i + 5
    nuclei = np.zeros(shape, dtype=np.int32)
    nuclei[(cell > 0) & (np.add.outer(np.arange(ny), np.arange(nx)) % 3 == 0)] = 1

    mask_path = tmp_path / "P001_cell_mask.npy"
    np.save(mask_path, cell)
    nuclei_path = tmp_path / "P001_nuclei_mask.npy"
    np.save(nuclei_path, nuclei)

    channel_paths = []
    rng = np.random.default_rng(7)
    for marker in markers:
        arr = (rng.random(shape) * 1000).astype(np.uint16)
        path = tmp_path / f"{marker}.tif"
        tifffile.imwrite(path, arr)
        channel_paths.append(path)
    return mask_path, nuclei_path, channel_paths, cell


def _morphology_csv(tmp_path: Path, cell: np.ndarray) -> Path:
    labels = np.unique(cell)
    labels = labels[labels != 0]
    df = pd.DataFrame(
        {
            "label": labels,
            "area": [int((cell == label).sum()) for label in labels],
            "centroid_x": labels.astype(float),
            "centroid_y": labels.astype(float),
        }
    )
    path = tmp_path / "morphology.csv"
    df.to_csv(path, index=False)
    return path


def _outputs(outdir: Path, channel_paths) -> list[Path]:
    """The per-marker CSV names Nextflow builds — `<patient>_<stem>_quant.csv`.

    The names are the CALLER's, in Python as in the pipeline: `modules/local/
    quantify.nf` renders them from `meta.channel_stem`, exactly as
    SPLIT_CHANNELS passes `--file-stems` rather than letting the script
    re-derive a stem. `bin/quantify.py` never invents an output name.
    """
    return [outdir / f"P001_{Path(p).stem}_quant.csv" for p in channel_paths]


# ── the instrumented loader ───────────────────────────────────────────────────
class _PlaneTracker:
    """Stands in for `quantify._load_channel` and records what stays alive.

    Every plane it hands out is registered in `live` and removed by a weakref
    finalizer at deallocation — under CPython that is the moment the last
    reference goes, so `len(live)` read as the NEXT load returns is exactly
    "how many earlier planes the implementation is still holding".
    """

    def __init__(self, shape=(700, 700), dtype=np.uint16):
        self.shape = shape
        self.dtype = dtype
        self.live: set[int] = set()
        self.live_at_load: list[int] = []
        self.bytes_at_load: list[int] = []
        self._sizes: dict[int, int] = {}

    @property
    def plane_bytes(self) -> int:
        return int(np.prod(self.shape)) * np.dtype(self.dtype).itemsize

    def __call__(self, path):
        arr = np.zeros(self.shape, dtype=self.dtype)
        key = id(arr)
        self.live.add(key)
        self._sizes[key] = arr.nbytes
        weakref.finalize(arr, self._release, key)
        self.live_at_load.append(len(self.live))
        self.bytes_at_load.append(sum(self._sizes[k] for k in self.live))
        return arr

    def _release(self, key: int) -> None:
        self.live.discard(key)
        self._sizes.pop(key, None)


def _batch(outdir, channel_paths, mask_path, nuclei_path=None, **kwargs):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    return quantify.run_quantification_batch(
        mask_path=str(mask_path),
        channel_paths=[str(p) for p in channel_paths],
        channel_names=[Path(p).stem for p in channel_paths],
        output_paths=[str(p) for p in _outputs(outdir, channel_paths)],
        nuclei_mask_path=str(nuclei_path) if nuclei_path else None,
        **kwargs,
    )


# ── 1. THE LOAD-BEARING TEST ──────────────────────────────────────────────────
def test_batch_never_holds_more_than_one_channel_plane(tmp_path, monkeypatch):
    """N channels, one plane resident. The whole point of the task.

    Fails against the stack-all-channels implementation with
    `live_at_load == [1, 2, 3, 4, 5, 6]` — every earlier plane still held while
    the next is read.
    """
    # Sixteen markers, the order of a real panel. The count matters: the naive
    # shape's excess is (N-1) planes, so a small N leaves the measured test
    # discriminating on a margin barely above its own noise.
    mask_path, nuclei_path, channel_paths, _ = _write_scene(
        tmp_path, markers=[f"M{i:02d}" for i in range(16)]
    )
    tracker = _PlaneTracker()
    monkeypatch.setattr(quantify, "_load_channel", tracker)
    # The masks must match the planes the tracker hands out, since the real
    # shape check still runs.
    monkeypatch.setattr(
        quantify,
        "_load_mask",
        lambda _path: np.ones(tracker.shape, dtype=np.int32),
    )

    _batch(tmp_path / "out", channel_paths, mask_path, nuclei_path)

    assert len(tracker.live_at_load) == len(channel_paths), (
        "every channel must be loaded exactly once; "
        f"{len(tracker.live_at_load)} loads for {len(channel_paths)} channels"
    )
    assert tracker.live_at_load == [1] * len(channel_paths), (
        "more than one channel plane was resident at once: "
        f"live planes at each load = {tracker.live_at_load}. The batch must "
        "load -> compute -> discard one channel at a time, never stack them."
    )
    assert max(tracker.bytes_at_load) == tracker.plane_bytes, (
        "peak tracked channel bytes "
        f"({max(tracker.bytes_at_load)}) exceeds one plane "
        f"({tracker.plane_bytes})"
    )


def test_batch_peak_allocation_does_not_scale_with_channel_count(
    tmp_path, monkeypatch
):
    """Peak allocated bytes must track ONE channel, not N.

    The instrumented test above cannot see a plane copied into a preallocated
    (N, Y, X) buffer — the source is freed, so the live-plane count stays 1
    while memory still grows with N. This one measures the allocator instead
    (numpy registers its data buffers with tracemalloc), so both shapes are
    covered.
    """
    # Sixteen markers, the order of a real panel. The count matters: the naive
    # shape's excess is (N-1) planes, so a small N leaves the measured test
    # discriminating on a margin barely above its own noise.
    mask_path, nuclei_path, channel_paths, _ = _write_scene(
        tmp_path, markers=[f"M{i:02d}" for i in range(16)]
    )
    tracker = _PlaneTracker()
    monkeypatch.setattr(quantify, "_load_channel", tracker)
    monkeypatch.setattr(
        quantify,
        "_load_mask",
        lambda _path: np.ones(tracker.shape, dtype=np.int32),
    )

    def peak_for(n_channels: int, tag: str) -> int:
        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            _batch(tmp_path / tag, channel_paths[:n_channels], mask_path, nuclei_path)
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

    one = peak_for(1, "one")
    many = peak_for(len(channel_paths), "many")
    # Headroom of two planes: enough to absorb the transient a single
    # load->compute->discard iteration needs, far short of the (N-1) extra
    # planes a stacking implementation would hold.
    budget = one + 2 * tracker.plane_bytes
    assert many <= budget, (
        f"peak for {len(channel_paths)} channels ({many} bytes) exceeds the "
        f"peak for 1 channel plus two planes ({budget} bytes). Peak memory is "
        "scaling with the marker count, which is exactly what batching must "
        "not do."
    )


# ── 2. EQUIVALENCE — the bytes must not move ─────────────────────────────────
def _per_marker(channel_paths, mask_path, nuclei_path, outdir, **kwargs):
    """Today's orchestration: one `run_quantification` call per marker."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    written = _outputs(outdir, channel_paths)
    for path, out in zip(channel_paths, written):
        quantify.run_quantification(
            mask_path=str(mask_path),
            channel_path=str(path),
            output_path=str(out),
            channel_name=Path(path).stem,
            nuclei_mask_path=str(nuclei_path) if nuclei_path else None,
            **kwargs,
        )
    return written


@pytest.mark.parametrize(
    "statistics", [["Median"], ["Median", "Mean", "Sum"], ["Median", "Mean Z"]]
)
def test_batched_per_marker_csvs_are_byte_identical(tmp_path, statistics):
    mask_path, nuclei_path, channel_paths, _ = _write_scene(tmp_path)

    one_by_one = _per_marker(
        channel_paths, mask_path, nuclei_path, tmp_path / "per_marker",
        statistics=statistics,
    )
    batched = _batch(
        tmp_path / "batched", channel_paths, mask_path, nuclei_path,
        statistics=statistics,
    )

    assert [Path(p).name for p in batched] == [p.name for p in one_by_one], (
        "the batch must write the SAME per-marker filenames the fan-out did — "
        "they are published paths"
    )
    for old, new in zip(one_by_one, batched):
        assert old.read_bytes() == Path(new).read_bytes(), (
            f"{old.name} differs between the per-marker and batched runs"
        )


def test_batched_merged_table_is_byte_identical(tmp_path):
    """The contract the brief states: identical AFTER the merge step."""
    mask_path, nuclei_path, channel_paths, cell = _write_scene(tmp_path)
    morphology = _morphology_csv(tmp_path, cell)

    one_by_one = _per_marker(
        channel_paths, mask_path, nuclei_path, tmp_path / "per_marker",
        statistics=["Median"],
    )
    batched = _batch(
        tmp_path / "batched", channel_paths, mask_path, nuclei_path,
        statistics=["Median"],
    )

    def merge(csvs, out):
        subprocess.run(
            [sys.executable, str(BIN / "merge_quant_csvs.py"),
             "--csv-files", *[str(c) for c in csvs],
             "--morphology", str(morphology),
             "--patient-id", "P001",
             "--output", str(out),
             "--nuclear-markers", "DAPI"],
            check=True, capture_output=True,
        )
        return out.read_bytes()

    assert merge(one_by_one, tmp_path / "merged_fanout.csv") == merge(
        batched, tmp_path / "merged_batch.csv"
    )


def test_batched_redsea_matches_the_per_marker_run(tmp_path):
    """REDSEA opts in per MARKER; batching must not widen or narrow that.

    The geometry is loaded once for the whole batch instead of once per marker
    task, so this also pins that sharing one geometry object across markers
    changes nothing — it is read-only.
    """
    mask_path, nuclei_path, channel_paths, cell = _write_scene(tmp_path)
    geom = redsea.redsea_geometry(cell, element_size=3)
    npz = tmp_path / "P001_redsea.npz"
    redsea_matrix.save_geometry(geom, npz)

    kwargs = dict(
        statistics=["Median", "REDSEA"],
        redsea_geometry_path=str(npz),
        redsea_markers=["CD3", "CD8"],   # two of six opt in
        redsea_checker=1,
    )
    one_by_one = _per_marker(
        channel_paths, mask_path, nuclei_path, tmp_path / "per_marker", **kwargs
    )
    batched = _batch(
        tmp_path / "batched", channel_paths, mask_path, nuclei_path, **kwargs
    )
    for old, new in zip(one_by_one, batched):
        assert old.read_bytes() == Path(new).read_bytes(), old.name
    # The opt-in is still per marker: only CD3/CD8 carry a REDSEA column.
    for path in batched:
        marker = Path(path).name[len("P001_"):-len("_quant.csv")]
        header = pd.read_csv(path, nrows=0).columns
        assert (f"{marker}: Cell: REDSEA" in header) == (marker in {"CD3", "CD8"})


# ── 3. the guards the batched shape needs ────────────────────────────────────
def test_ragged_parallel_lists_are_refused(tmp_path):
    """Three parallel lists can drift, so the drift is an error, not a zip().

    `zip()` truncates to the shortest list SILENTLY, which here would mean a
    patient's last markers vanishing from the merged table with the run still
    exiting 0 — the failure class this branch exists to remove.
    """
    mask_path, nuclei_path, channel_paths, _ = _write_scene(tmp_path)
    with pytest.raises(ValueError, match="channel"):
        quantify.run_quantification_batch(
            mask_path=str(mask_path),
            channel_paths=[str(p) for p in channel_paths],
            channel_names=[Path(p).stem for p in channel_paths][:-1],
            output_paths=[str(p) for p in _outputs(tmp_path, channel_paths)],
            nuclei_mask_path=str(nuclei_path),
        )


def test_colliding_output_paths_are_refused(tmp_path):
    """Two markers whose output paths collide would overwrite one another.

    Distinct DECLARED names can sanitise to the same file stem ('CD3.105' and
    'CD3_105' both give 'CD3_105'), and the CSV is named from the stem. Under
    the per-marker fan-out that was two tasks publishing to one path — a silent
    last-writer-wins. In ONE task it is one file written twice, which is worse
    (nothing downstream can even see two CSVs arrived), so it is refused by
    name.
    """
    mask_path, nuclei_path, channel_paths, _ = _write_scene(tmp_path)
    same = str(tmp_path / "P001_CD3_105_quant.csv")
    with pytest.raises(ValueError, match="P001_CD3_105_quant.csv"):
        quantify.run_quantification_batch(
            mask_path=str(mask_path),
            channel_paths=[str(channel_paths[0]), str(channel_paths[1])],
            channel_names=["CD3.105", "CD3_105"],
            output_paths=[same, same],
            nuclei_mask_path=str(nuclei_path),
        )
