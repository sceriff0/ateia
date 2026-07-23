# Low-Memory Parallel Registration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let mirage register full-resolution WSIs on a low-resource machine by replacing VALIS's eager BioFormats slide reader with a lazy pyvips one and splitting the full-res warp into independent output tiles, without changing any VALIS algorithm.

**Architecture:** VALIS's `warp_slide()` is `reader.slide2vips()` + `warp_tools.warp_img()`. The warp is already lazy pyvips; only the read is eager. We supply a lazy, JVM-free reader through VALIS's supported `Valis.register(reader_cls=...)` hook and through our own `reg_finalize.py`, which makes the whole pipeline stream in `O(tile)` RAM. Because pyvips is demand-driven, an output-tile task can run the identical warp and `.crop()` it — bit-identical by construction.

**Tech Stack:** Nextflow DSL2 (>=25.04.0), Python 3, pyvips/libvips, tifffile, VALIS 1.0.0 (`valis-wsi`), Docker/Singularity, pytest, nf-test.

## Global Constraints

- **Branch:** `feature/reg-lowmem-parallel`. Already created; base commit `9ad0ff7`.
- **Spec:** `docs/superpowers/specs/2026-07-22-lowmem-parallel-registration-design.md`. Read it before Task 1.
- **Never modify `valis_lib/`.** It is a pristine read-only reference copy of PyPI `valis-wsi` 1.0.0. Verified byte-identical. Changes there fool nobody and break the faithfulness argument.
- **Never change a VALIS algorithm.** This work changes readers and process boundaries only.
- **Container for all registration work:** `bolt3x/attend_image_analysis:mirage_valis_1.0.0`.
- **pytest is NOT installed in that container.** Tests that must run inside it need a stdlib `__main__` runner (see `tests/unit/test_tile_grid.py` for the established pattern). Tests that run in CI outside the container use pytest with `pytest.importorskip`.
- **Executable bit:** any `bin/*.py` invoked **by name** from a Nextflow process must be git-mode `100755` or it fails at runtime with exit 126 `Permission denied`. Run `git update-index --chmod=+x bin/<script>.py` and confirm with `git ls-files -s bin/<script>.py`. A local `chmod` alone does not reach the cluster checkout. Import-only modules under `bin/utils/` stay `100644`.
- **Commit style:** gitmoji `:shortcode:` prefix on every commit (`:sparkles:` feature, `:bug:` fix, `:white_check_mark:` tests, `:wrench:` config, `:memo:` docs, `:recycle:` refactor).
- **Never `git add -A`.** Another agent may share this worktree and switch branches mid-session. Stage explicit paths only, and run `git rev-parse --abbrev-ref HEAD` before every commit to confirm you are still on `feature/reg-lowmem-parallel`.
- **Every new Nextflow process** needs: `tag`, `label`, `container`, `[meta, ...]` tuple I/O, a `versions.yml` emit, and a `stub:` block.
- **Process args** go in `conf/modules.config` via `ext.args`, never hardcoded in the process script.
- **Default behaviour must not change.** `params.reg_distributed_tiling` stays `false`. Everything here is opt-in.

## File Structure

**Created:**

| Path | Responsibility | Mode |
|---|---|---|
| `bin/utils/mirage_slide_reader.py` | JVM-free VALIS-compatible reader for mirage's own tiled OME-TIFF + a fallback factory | 100644 |
| `bin/reg_warp_tile.py` | Warp one output tile of one slide | 100755 |
| `bin/reg_assemble.py` | Join warped tiles into the final OME-TIFF | 100755 |
| `bin/compare_registration.py` | Stream-compare two registered slides, emit JSON + diff PNG | 100755 |
| `modules/local/reg_warp_tile.nf` | `REG_WARP_TILE` process | — |
| `modules/local/reg_assemble.nf` | `REG_ASSEMBLE` process | — |
| `modules/local/compare_registration.nf` | `COMPARE_REGISTRATION` process | — |
| `subworkflows/local/reg_compare.nf` | Run both adapters, join, compare | — |
| `tests/unit/test_mirage_slide_reader.py` | Reader round-trip correctness | — |
| `tests/integration/probe_reader_equivalence.py` | Assumption A1/A2 probe, kept as a regression test | — |
| `tests/integration/verify_lowmem_bitidentical.py` | Two legs: reader-swap vs classic, tiles vs single warp | — |
| `tests/modules/reg_warp_tile.nf.test`, `tests/modules/reg_assemble.nf.test` | stub wiring | — |

**Modified:**

| Path | Change |
|---|---|
| `bin/utils/valis_config.py` | reader selection helper, `init_jvm` becomes conditional |
| `bin/reg_prep.py:117-118` | pass `reader_cls=`, skip JVM when possible; emit stage timings |
| `bin/reg_micro_prep.py` | same reader/JVM treatment |
| `bin/reg_finalize.py:208-255` | lazy reader; split warp out behind `--emit-field-only`; pyvips writer primary |
| `subworkflows/local/adapters/valis_distributed_adapter.nf` | wire tile fan-out + assemble, incl. `REG_WARP_REF` |
| `subworkflows/local/registration.nf:179-214` | extract adapter selection helper; add `--reg_compare` branch |
| `subworkflows/local/add_cycle.nf:26,68` | use the shared adapter selector |
| `lib/ParamUtils.groovy` | fast-fail `reg_qc=2` + new path |
| `conf/modules.config:44,207-262` | `reg_mem_budget_gb`, fix `REG_NONRIGID`/`REG_MICRO_PREP` sizing |
| `nextflow.config` | new params |
| `.github/workflows/ci.yml:346` | add the new integration legs + wire `verify_micro_bitidentical.py` |

---

### Task 0: Probe assumptions A1 and A2

The strongest faithfulness claim rests on pyvips and BioFormats decoding the same pixels, and on `reader_cls=` actually keeping the JVM out. Prove both before building on them. The probe stays in the repo as a regression test.

**Files:**
- Create: `tests/integration/probe_reader_equivalence.py`

**Interfaces:**
- Consumes: nothing.
- Produces: a verified answer to A1/A2. If A1 fails, STOP and report — the guarantee in the spec drops from "bit-identical by construction" to "empirically close", and the user must decide whether to continue.

- [ ] **Step 1: Write the probe**

Create `tests/integration/probe_reader_equivalence.py`:

```python
#!/usr/bin/env python3
"""Probe spec assumptions A1 and A2 before implementing the lazy reader.

A1: pyvips and BioFormats decode mirage's preprocessed OME-TIFF to IDENTICAL pixels.
A2: Valis.register(reader_cls=...) keeps the BioFormats JVM out of the rigid stage.

Usage (inside the VALIS image):
  docker run --rm -v "$PWD":/work -w /work \
      bolt3x/attend_image_analysis:mirage_valis_1.0.0 \
      python3 tests/integration/probe_reader_equivalence.py
"""
import os
import sys

import numpy as np
import pyvips
import tifffile

WORK = "/tmp/probe_reader"
SRC = os.path.join(WORK, "probe.ome.tiff")
H, W, C = 512, 640, 3


def make_fixture():
    """Write a fixture with mirage's EXACT writer settings (bin/preprocess.py:391-399)."""
    os.makedirs(WORK, exist_ok=True)
    rng = np.random.default_rng(0)
    arr = rng.integers(0, 65535, size=(C, H, W), dtype=np.uint16)
    tifffile.imwrite(
        SRC, arr,
        photometric="minisblack",
        metadata={"axes": "CYX"},
        bigtiff=True,
        ome=True,
        compression="zlib",
        tile=(256, 256),
    )
    return arr


def read_pyvips():
    """Lazy multi-page read: toilet-roll -> per-band crop -> bandjoin."""
    img = pyvips.Image.new_from_file(SRC, access="random", n=-1)
    ph = img.get("page-height")
    bands = [img.crop(0, i * ph, img.width, ph) for i in range(img.height // ph)]
    joined = bands[0] if len(bands) == 1 else bands[0].bandjoin(bands[1:])
    mem = np.frombuffer(joined.write_to_memory(), dtype=np.uint16)
    return mem.reshape(joined.height, joined.width, joined.bands)


def read_bioformats():
    from valis import slide_io
    reader_cls = slide_io.get_slide_reader(SRC, series=0)
    reader = reader_cls(SRC, series=0)
    vips_img = reader.slide2vips(level=0)
    mem = np.frombuffer(vips_img.write_to_memory(), dtype=np.uint16)
    return mem.reshape(vips_img.height, vips_img.width, vips_img.bands), reader_cls.__name__


def main():
    truth = make_fixture()
    truth_hwc = np.moveaxis(truth, 0, -1)

    pv = read_pyvips()
    print(f"[A1] pyvips shape={pv.shape} vs tifffile {truth_hwc.shape}", flush=True)
    a1_vs_truth = np.array_equal(pv, truth_hwc)
    print(f"[A1] pyvips == tifffile source array: {a1_vs_truth}", flush=True)

    from valis import registration
    registration.init_jvm(mem_gb=8)
    bf, reader_name = read_bioformats()
    print(f"[A1] BioFormats reader class chosen: {reader_name}", flush=True)
    a1_bf = bf.shape == pv.shape and np.array_equal(bf, pv)
    d = None if bf.shape != pv.shape else float(np.max(np.abs(bf.astype(np.int64) - pv.astype(np.int64))))
    print(f"[A1] pyvips == BioFormats: {a1_bf}  max|delta|={d}", flush=True)
    registration.kill_jvm()

    print("=" * 72)
    print(f"A1 VERDICT: {'PASS' if (a1_vs_truth and a1_bf) else 'FAIL'}")
    print("=" * 72, flush=True)
    return 0 if (a1_vs_truth and a1_bf) else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run the probe**

```bash
docker run --rm -v "$PWD":/work -w /work \
  bolt3x/attend_image_analysis:mirage_valis_1.0.0 \
  python3 tests/integration/probe_reader_equivalence.py
```

Expected: `A1 VERDICT: PASS`, and `[A1] BioFormats reader class chosen: BioFormatsSlideReader` (confirming multichannel does route to BioFormats today, per `valis_lib/slide_io.py:2418-2424`).

- [ ] **Step 3: If A1 FAILS, stop and report**

Do not proceed. Report to the user: the exact `max|delta|`, the reader class chosen, and the compression in use. The spec's §3 guarantee must be downgraded and the user decides whether to continue with an "empirically close" target measured by `--reg_compare`.

- [ ] **Step 4: Commit**

```bash
git rev-parse --abbrev-ref HEAD   # must print feature/reg-lowmem-parallel
git add tests/integration/probe_reader_equivalence.py
git commit -m ":white_check_mark: Probe pyvips/BioFormats decode equivalence (spec A1)"
```

---

### Task 1: The lazy reader

**Files:**
- Create: `bin/utils/mirage_slide_reader.py`
- Test: `tests/unit/test_mirage_slide_reader.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class MirageVipsSlideReader(slide_io.SlideReader)` with `__init__(src_f, series=None)`, `.metadata` (a `slide_io.MetaData`), `.slide2vips(level=0, series=None, xywh=None) -> pyvips.Image`, `.slide2image(level=0, series=None, xywh=None) -> np.ndarray`, `.scale_physical_size(level) -> list`.
  - `MirageVipsSlideReader.can_read(src_f) -> bool` (staticmethod).
  - `get_reader_for(src_f, series=None) -> type` — returns `MirageVipsSlideReader` when `can_read`, else `slide_io.get_slide_reader(src_f, series=series)`.
  - `all_readable(paths) -> bool` — True when every path is `can_read`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_mirage_slide_reader.py`:

```python
"""MirageVipsSlideReader must round-trip mirage's own OME-TIFF writer exactly."""
import numpy as np
import pytest

pyvips = pytest.importorskip("pyvips")
tifffile = pytest.importorskip("tifffile")

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin", "utils"))


H, W, C = 300, 384, 4


@pytest.fixture
def slide(tmp_path):
    """A fixture written with bin/preprocess.py's exact settings."""
    rng = np.random.default_rng(7)
    arr = rng.integers(0, 65535, size=(C, H, W), dtype=np.uint16)
    p = tmp_path / "s.ome.tiff"
    tifffile.imwrite(
        str(p), arr,
        photometric="minisblack",
        metadata={"axes": "CYX", "Channel": {"Name": ["DAPI", "SMA", "PANCK", "CD3"]}},
        bigtiff=True, ome=True, compression="zlib", tile=(256, 256),
    )
    return str(p), arr


def test_can_read_accepts_mirage_output(slide):
    from mirage_slide_reader import MirageVipsSlideReader
    path, _ = slide
    assert MirageVipsSlideReader.can_read(path) is True


def test_slide2vips_matches_source_array(slide):
    from mirage_slide_reader import MirageVipsSlideReader
    path, arr = slide
    img = MirageVipsSlideReader(path).slide2vips(level=0)
    assert (img.width, img.height, img.bands) == (W, H, C)
    got = np.frombuffer(img.write_to_memory(), dtype=np.uint16).reshape(H, W, C)
    assert np.array_equal(got, np.moveaxis(arr, 0, -1))


def test_slide2vips_region_matches_full_crop(slide):
    from mirage_slide_reader import MirageVipsSlideReader
    path, arr = slide
    r = MirageVipsSlideReader(path)
    region = r.slide2vips(level=0, xywh=(10, 20, 64, 48))
    got = np.frombuffer(region.write_to_memory(), dtype=np.uint16).reshape(48, 64, C)
    assert np.array_equal(got, np.moveaxis(arr, 0, -1)[20:68, 10:74, :])


def test_metadata_reports_dims_and_channels(slide):
    from mirage_slide_reader import MirageVipsSlideReader
    path, _ = slide
    md = MirageVipsSlideReader(path).metadata
    assert md.slide_dimensions[0] == (W, H)
    assert md.n_channels == C
    assert md.channel_names == ["DAPI", "SMA", "PANCK", "CD3"]
    assert md.is_rgb is False


def test_can_read_rejects_untiled(tmp_path):
    from mirage_slide_reader import MirageVipsSlideReader
    p = tmp_path / "flat.tiff"
    tifffile.imwrite(str(p), np.zeros((32, 32), dtype=np.uint16))
    assert MirageVipsSlideReader.can_read(str(p)) is False
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/unit/test_mirage_slide_reader.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'mirage_slide_reader'`.

- [ ] **Step 3: Write the implementation**

Create `bin/utils/mirage_slide_reader.py`:

```python
"""JVM-free VALIS-compatible slide reader for mirage's own tiled OME-TIFF output.

Why this exists
---------------
VALIS routes multichannel OME-TIFF to ``BioFormatsSlideReader`` (valis slide_io, reader
dispatch), whose ``slide2vips`` decodes every tile through the JVM and stitches them --
materializing the whole decompressed slide. That is mirage's registration RAM wall.
VALIS's own comment says the reason is SPEED ("very slow for multichannel"), not correctness.

mirage always writes its own inputs: ``bin/preprocess.py`` emits tiled (2048x2048) BigTIFF
OME-TIFF, and ``bin/reg_finalize.py::_save_ome_pyvips`` writes tiled BigTIFF too. Those are
randomly addressable, so pyvips can read them LAZILY with no JVM. Since
``valis.warp_tools.warp_img`` is already pure lazy pyvips, handing VALIS a lazy reader makes
the whole warp stream in O(tile) RAM with no algorithm change.

Anything this reader does not recognise falls back to VALIS's own dispatch via
``get_reader_for``, so non-mirage inputs keep working exactly as before.
"""
import os

import numpy as np
import pyvips

from valis import slide_io

# Bands are packed vertically into pages by both tifffile (ome=True, axes="CYX") and
# reg_finalize._save_ome_pyvips (page-height set explicitly). Reading with n=-1 yields a
# "toilet roll" of height C*page_height which we crop back into bands.
_TOILET_ROLL = -1


def _open_roll(src_f):
    return pyvips.Image.new_from_file(str(src_f), access="random", n=_TOILET_ROLL)


def _page_height(img):
    try:
        return int(img.get("page-height"))
    except pyvips.Error:
        return int(img.height)


def _bandjoin_pages(img):
    """Turn a vertically-packed multi-page image into a multi-band image (lazy)."""
    ph = _page_height(img)
    n_pages = img.height // ph
    if n_pages <= 1:
        return img
    bands = [img.crop(0, i * ph, img.width, ph) for i in range(n_pages)]
    return bands[0].bandjoin(bands[1:])


class MirageVipsSlideReader(slide_io.SlideReader):
    """Lazy, JVM-free reader for mirage-produced tiled OME-TIFFs."""

    def __init__(self, src_f, series=None, *args, **kwargs):
        super().__init__(src_f, *args, **kwargs)
        self.src_f = str(src_f)
        self.series = 0 if series is None else int(series)
        self._img = _bandjoin_pages(_open_roll(self.src_f))
        self.metadata = self._build_metadata()

    # -- VALIS SlideReader API -------------------------------------------------

    def slide2vips(self, level=0, series=None, xywh=None, *args, **kwargs):
        if level != 0:
            # mirage's OME-TIFFs are single-level (bin/preprocess.py writes no subifds).
            raise ValueError(f"MirageVipsSlideReader has no pyramid level {level}")
        img = self._img
        if xywh is not None:
            x, y, w, h = (int(v) for v in xywh)
            img = img.crop(x, y, w, h)
        return img

    def slide2image(self, level=0, series=None, xywh=None, *args, **kwargs):
        img = self.slide2vips(level=level, series=series, xywh=xywh)
        arr = np.frombuffer(img.write_to_memory(),
                            dtype=slide_io.vips2numpy_dtype(img.format)
                            if hasattr(slide_io, "vips2numpy_dtype") else _vips_dtype(img.format))
        arr = arr.reshape(img.height, img.width, img.bands)
        return arr[..., 0] if img.bands == 1 else arr

    def scale_physical_size(self, level):
        return list(self.metadata.pixel_physical_size_xyu)

    # -- construction helpers --------------------------------------------------

    def _build_metadata(self):
        md = slide_io.MetaData(os.path.basename(self.src_f), "mirage-pyvips", series=self.series)
        md.slide_dimensions = [(self._img.width, self._img.height)]
        md.n_channels = self._img.bands
        md.is_rgb = False
        md.channel_names = _channel_names(self.src_f, self._img.bands)
        md.pixel_physical_size_xyu = _physical_size(self.src_f)
        md.original_xml = _ome_xml(self.src_f)
        md.bf_datatype = _bf_dtype(self._img.format)
        md.optimal_tile_wh = _tile_wh(self.src_f)
        return md

    @staticmethod
    def can_read(src_f):
        """True only for tiled TIFFs this reader is known to handle correctly."""
        try:
            import tifffile
            with tifffile.TiffFile(str(src_f)) as tf:
                page = tf.pages[0]
                if not page.is_tiled:
                    return False
            img = _bandjoin_pages(_open_roll(src_f))
            return img.width > 0 and img.height > 0 and img.bands >= 1
        except Exception:
            return False


def _vips_dtype(vips_format):
    return {
        "uchar": np.uint8, "char": np.int8, "ushort": np.uint16, "short": np.int16,
        "uint": np.uint32, "int": np.int32, "float": np.float32, "double": np.float64,
    }[vips_format]


def _bf_dtype(vips_format):
    return {
        "uchar": "uint8", "char": "int8", "ushort": "uint16", "short": "int16",
        "uint": "uint32", "int": "int32", "float": "float", "double": "double",
    }[vips_format]


def _ome_xml(src_f):
    import tifffile
    with tifffile.TiffFile(str(src_f)) as tf:
        return tf.ome_metadata


def _channel_names(src_f, n_bands):
    xml = _ome_xml(src_f)
    if not xml:
        return [f"C{i}" for i in range(n_bands)]
    import re
    names = re.findall(r'<Channel[^>]*\bName="([^"]*)"', xml)
    if len(names) < n_bands:
        names = list(names) + [f"C{i}" for i in range(len(names), n_bands)]
    return names[:n_bands]


def _physical_size(src_f):
    xml = _ome_xml(src_f)
    if not xml:
        return [1.0, 1.0, slide_io.PIXEL_UNIT]
    import re
    x = re.search(r'PhysicalSizeX="([^"]+)"', xml)
    y = re.search(r'PhysicalSizeY="([^"]+)"', xml)
    u = re.search(r'PhysicalSizeXUnit="([^"]+)"', xml)
    if not (x and y):
        return [1.0, 1.0, slide_io.PIXEL_UNIT]
    return [float(x.group(1)), float(y.group(1)), u.group(1) if u else "µm"]


def _tile_wh(src_f):
    import tifffile
    with tifffile.TiffFile(str(src_f)) as tf:
        tw = tf.pages[0].tilewidth
    return int(tw) if tw else 1024


def get_reader_for(src_f, series=None):
    """Return the cheapest correct reader class for `src_f`.

    MirageVipsSlideReader when we recognise the file (no JVM, lazy); otherwise VALIS's own
    dispatch, which starts the JVM and picks BioFormats.
    """
    if MirageVipsSlideReader.can_read(src_f):
        return MirageVipsSlideReader
    return slide_io.get_slide_reader(str(src_f), series=series)


def all_readable(paths):
    """True when every path can be read JVM-free (so the caller can skip init_jvm entirely)."""
    return all(MirageVipsSlideReader.can_read(p) for p in paths)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
pytest tests/unit/test_mirage_slide_reader.py -v
```
Expected: 5 passed. If pyvips is unavailable locally, run inside the container instead:
```bash
docker run --rm -v "$PWD":/work -w /work bolt3x/attend_image_analysis:mirage_valis_1.0.0 \
  python3 -c "import sys; sys.path.insert(0,'bin/utils'); import mirage_slide_reader; print('import ok')"
```

- [ ] **Step 5: Commit**

```bash
git rev-parse --abbrev-ref HEAD
git add bin/utils/mirage_slide_reader.py tests/unit/test_mirage_slide_reader.py
git commit -m ":sparkles: Add JVM-free lazy pyvips slide reader for mirage OME-TIFF"
```

---

### Task 2: Use the lazy reader in the full-res warp

This is where the RAM win lands. `reg_finalize.py` currently calls `slide_tools.warp_slide`, which internally calls `slide_io.get_slide_reader` — BioFormats. Replace with an explicit lazy read + the same `warp_tools.warp_img`.

**Files:**
- Modify: `bin/reg_finalize.py:208-255`
- Test: `tests/integration/verify_lowmem_bitidentical.py` (leg 1)

**Interfaces:**
- Consumes: `mirage_slide_reader.get_reader_for`, `MirageVipsSlideReader`.
- Produces: `reg_finalize.warp_source(src_slide, ws, dxdy) -> pyvips.Image` — the lazy warped full image, callable by `reg_warp_tile.py` in Task 4.

- [ ] **Step 1: Add the warp helper**

In `bin/reg_finalize.py`, after the existing imports, add:

```python
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "utils"))
from mirage_slide_reader import get_reader_for, all_readable   # noqa: E402
```

Then add this function above `main()`:

```python
def warp_source(src_slide, ws, dxdy):
    """Lazily warp `src_slide` per the dumped warp state. Returns an UNEVALUATED pyvips.Image.

    Identical to valis.slide_tools.warp_slide, except the slide is read through get_reader_for
    (lazy pyvips when possible) instead of always BioFormats. The warp itself is VALIS's own
    warp_tools.warp_img -- unchanged. Because the result is lazy, callers may .crop() a region
    and only that region's source pixels are ever decoded.
    """
    reader_cls = get_reader_for(src_slide, series=ws.get("series"))
    reader = reader_cls(src_slide, series=ws.get("series"))
    vips_slide = reader.slide2vips(level=0, series=ws.get("series"))
    return warp_tools.warp_img(
        img=vips_slide,
        M=np.asarray(ws["M"]),
        bk_dxdy=dxdy,
        transformation_dst_shape_rc=tuple(ws["reg_img_shape_rc"]),
        out_shape_rc=tuple(ws["aligned_slide_shape_rc"]),
        transformation_src_shape_rc=tuple(ws["processed_img_shape_rc"]),
        bbox_xywh=tuple(ws["bbox_xywh"]) if ws.get("bbox_xywh") else None,
        bg_color=ws.get("bg_color"),
        interp_method=ws.get("interp_method", "bicubic"),
    ), reader
```

Add `from valis import warp_tools` to the imports if not already present.

- [ ] **Step 2: Replace the warp + reader in `main()`**

In `bin/reg_finalize.py`, replace the block at lines 208-226 (the `slide_tools.warp_slide(...)` call plus the two `slide_io.get_slide_reader` lines that follow) with:

```python
    # 4) warp the full-res slide lazily (VALIS's own warp_img; only the READER differs)
    warped, reader = warp_source(args.src_slide, ws, slide_bk)

    # 5) save as OME-TIFF, mirroring Slide.warp_and_save_slide's metadata handling
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    slide_meta = reader.metadata
```

Leave the rest of the save block unchanged.

- [ ] **Step 3: Make the JVM conditional**

In `bin/reg_finalize.py`, replace the unconditional `init_jvm` call (around line 171) with:

```python
    if all_readable([args.src_slide]):
        heap_gb = 0
        print("[reg_finalize] all inputs readable by MirageVipsSlideReader; skipping JVM", flush=True)
    else:
        heap_gb = init_jvm(os.path.dirname(os.path.abspath(args.src_slide)) or ".",
                           override_gb=args.jvm_heap_gb)
        print(f"[reg_finalize] started BioFormats JVM (heap={heap_gb}GB)", flush=True)
```

and guard the teardown at the end of `main()`:

```python
    if heap_gb:
        registration.kill_jvm()
```

- [ ] **Step 4: Write the failing integration test (leg 1)**

Create `tests/integration/verify_lowmem_bitidentical.py`:

```python
#!/usr/bin/env python3
"""Verify the low-memory registration path is bit-identical to what it replaces.

Leg 1 (this task): reg_finalize with the lazy pyvips reader == reg_finalize with BioFormats.
Leg 2 (Task 4):    tile fan-out + assemble == the single-process warp.

Usage (inside the VALIS image):
  docker run --rm -v "$PWD":/work -w /work \
      bolt3x/attend_image_analysis:mirage_valis_1.0.0 \
      python3 tests/integration/verify_lowmem_bitidentical.py
"""
import json
import os
import shutil
import subprocess
import sys

import numpy as np
import pyvips

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "bin", "utils"))

WORK = "/tmp/verify_lowmem"
INP = os.path.join(WORK, "in")
PREP = os.path.join(WORK, "prep")
REF, MOV = "P001_ref.ome.tiff", "P001_mov1.ome.tiff"
MOV_STEM = "P001_mov1"


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write("\n".join((r.stdout + r.stderr).splitlines()[-15:]))
        raise SystemExit(f"FAILED: {' '.join(cmd[1:4])}")
    return r


def px(path):
    from valis import warp_tools
    return warp_tools.vips2numpy(pyvips.Image.new_from_file(path)).astype(np.float64)


def setup():
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(INP)
    for s in (REF, MOV):
        shutil.copy(os.path.join("tests/testdata", s), os.path.join(INP, s))
    run([sys.executable, "bin/reg_prep.py", "--input-dir", INP, "--out", PREP,
         "--reference", REF, "--memory-mode", "low", "--skip-micro-registration"])
    return os.path.join(PREP, MOV_STEM)


def leg1(md):
    """Lazy reader vs BioFormats, same field, same warp state."""
    ti = os.path.join(md, "tiler_inputs")
    ws = os.path.join(md, "warp_state.json")
    field = os.path.join(md, "nr", "bk.v")
    os.makedirs(os.path.join(md, "nr"), exist_ok=True)
    run([sys.executable, "bin/reg_nonrigid.py", "--inputs-dir", ti,
         "--out-dir", os.path.join(md, "nr")])

    lazy_out = os.path.join(md, "lazy.ome.tiff")
    bf_out = os.path.join(md, "bf.ome.tiff")
    run([sys.executable, "bin/reg_finalize.py", "--inputs-dir", ti, "--field", field,
         "--warp-state", ws, "--src-slide", os.path.join(INP, MOV), "--out", lazy_out])
    env = dict(os.environ, MIRAGE_FORCE_BIOFORMATS="1")
    r = subprocess.run([sys.executable, "bin/reg_finalize.py", "--inputs-dir", ti, "--field", field,
                        "--warp-state", ws, "--src-slide", os.path.join(INP, MOV), "--out", bf_out],
                       capture_output=True, text=True, env=env)
    if r.returncode != 0:
        sys.stderr.write("\n".join((r.stdout + r.stderr).splitlines()[-15:]))
        raise SystemExit("FAILED: BioFormats reference run")

    a, b = px(lazy_out), px(bf_out)
    equal = a.shape == b.shape and np.array_equal(a, b)
    d = None if a.shape != b.shape else float(np.max(np.abs(a - b)))
    print("=" * 72)
    print(f"LEG 1 lazy-reader vs BioFormats: equal={equal} max|delta|={d}")
    print("=" * 72, flush=True)
    return 0 if equal else 1


def main():
    md = setup()
    return leg1(md)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Add the escape hatch the test depends on**

In `bin/utils/mirage_slide_reader.py`, make `get_reader_for` honour an override so the test can force the old path:

```python
def get_reader_for(src_f, series=None):
    if os.environ.get("MIRAGE_FORCE_BIOFORMATS") == "1":
        return slide_io.get_slide_reader(str(src_f), series=series)
    if MirageVipsSlideReader.can_read(src_f):
        return MirageVipsSlideReader
    return slide_io.get_slide_reader(str(src_f), series=series)
```

and make `all_readable` respect it too:

```python
def all_readable(paths):
    if os.environ.get("MIRAGE_FORCE_BIOFORMATS") == "1":
        return False
    return all(MirageVipsSlideReader.can_read(p) for p in paths)
```

- [ ] **Step 6: Run leg 1**

```bash
docker run --rm -v "$PWD":/work -w /work \
  bolt3x/attend_image_analysis:mirage_valis_1.0.0 \
  python3 tests/integration/verify_lowmem_bitidentical.py
```
Expected: `LEG 1 lazy-reader vs BioFormats: equal=True max|delta|=0.0`.

- [ ] **Step 7: Commit**

```bash
git rev-parse --abbrev-ref HEAD
git add bin/reg_finalize.py bin/utils/mirage_slide_reader.py tests/integration/verify_lowmem_bitidentical.py
git commit -m ":zap: Warp full-res slides through the lazy pyvips reader (no JVM)"
```

---

### Task 3: Use the lazy reader in the rigid stages

**Files:**
- Modify: `bin/utils/valis_config.py` (add `maybe_init_jvm`)
- Modify: `bin/reg_prep.py:73-76, 117-118, 211-217`
- Modify: `bin/reg_micro_prep.py:111-113, 238-243`

**Interfaces:**
- Consumes: `mirage_slide_reader.get_reader_for`, `all_readable`.
- Produces: `valis_config.maybe_init_jvm(input_dir, override_gb=None) -> int` — returns `0` and starts no JVM when every slide in `input_dir` is mirage-readable, otherwise behaves exactly like the existing `init_jvm`.

- [ ] **Step 1: Add `maybe_init_jvm`**

In `bin/utils/valis_config.py`, append:

```python
def slide_paths(input_dir):
    """Every slide file in `input_dir`, matching init_jvm's extension filter."""
    return [os.path.join(input_dir, f) for f in sorted(os.listdir(input_dir))
            if f.lower().endswith((".tif", ".tiff", ".ome.tif", ".ome.tiff"))]


def maybe_init_jvm(input_dir, override_gb=None):
    """Start the BioFormats JVM only when some input needs it.

    Returns the heap size in GB, or 0 when no JVM was started. mirage's own preprocessed
    OME-TIFFs are read JVM-free by MirageVipsSlideReader, so on a normal run this starts
    nothing -- which is the entire point of the low-memory path.
    """
    from mirage_slide_reader import all_readable
    paths = slide_paths(input_dir)
    if paths and all_readable(paths):
        return 0
    return init_jvm(input_dir, override_gb=override_gb)
```

`valis_config.py` already imports `os`. Add `sys.path` handling only if the module cannot already resolve `mirage_slide_reader` — both live in `bin/utils/`, which the callers put on `sys.path`, so a plain import is correct.

- [ ] **Step 2: Switch `reg_prep.py` to the conditional JVM and the injected reader**

Replace `bin/reg_prep.py:73-76`:

```python
    # Size + start the BioFormats JVM only if some input is NOT mirage-readable.
    heap = maybe_init_jvm(args.input_dir, override_gb=args.jvm_heap_gb)
    print(f"[reg_prep] JVM heap = {heap} GB" if heap else
          "[reg_prep] all inputs readable by MirageVipsSlideReader; no JVM started", flush=True)
```

Change the import on line 39 to:

```python
from valis_config import build_registrar_kwargs, maybe_init_jvm
from mirage_slide_reader import get_reader_for, MirageVipsSlideReader, all_readable
```

Replace `bin/reg_prep.py:117-118`:

```python
        reg = registration.Valis(args.input_dir, args.out, **kwargs)
        reader_cls = (MirageVipsSlideReader
                      if all_readable(slide_paths(args.input_dir)) else None)
        reg.register(reader_cls=reader_cls)
```

Import `slide_paths` alongside the others. Guard both `registration.kill_jvm()` calls (lines 211, 217) with `if heap:`.

- [ ] **Step 3: Apply the identical change to `reg_micro_prep.py`**

Same three edits at `bin/reg_micro_prep.py:46` (imports), `:111-113` (JVM), and its `registrar.register(...)` call and `kill_jvm()` calls at `:238, :243`. The `register()` call there takes the same `reader_cls=` keyword.

- [ ] **Step 4: Verify A2 empirically**

```bash
docker run --rm -v "$PWD":/work -w /work \
  bolt3x/attend_image_analysis:mirage_valis_1.0.0 bash -lc '
    python3 tests/testdata/generate_complete_testdata.py >/dev/null 2>&1 || true
    mkdir -p /tmp/a2 && cp tests/testdata/P001_*.ome.tiff /tmp/a2/
    python3 bin/reg_prep.py --input-dir /tmp/a2 --out /tmp/a2out \
      --reference P001_ref.ome.tiff --memory-mode low --skip-micro-registration 2>&1 | tail -20'
```
Expected: the line `[reg_prep] all inputs readable by MirageVipsSlideReader; no JVM started`, and **no** BioFormats/JVM banner anywhere in the output. If a JVM banner appears, assumption A2 is violated — find the offending `get_slide_reader` call site and report before proceeding.

- [ ] **Step 5: Confirm the rigid result is unchanged**

```bash
docker run --rm -v "$PWD":/work -w /work \
  bolt3x/attend_image_analysis:mirage_valis_1.0.0 \
  python3 tests/integration/verify_lowmem_bitidentical.py
```
Expected: leg 1 still `equal=True max|delta|=0.0` (it re-runs `reg_prep` in `setup()`).

- [ ] **Step 6: Commit**

```bash
git rev-parse --abbrev-ref HEAD
git add bin/utils/valis_config.py bin/reg_prep.py bin/reg_micro_prep.py
git commit -m ":zap: Inject the lazy reader into the rigid stages; skip the JVM entirely"
```

---

### Task 4: Split the warp into output tiles

**Files:**
- Create: `bin/reg_warp_tile.py` (mode 100755)
- Create: `bin/reg_assemble.py` (mode 100755)
- Modify: `bin/reg_finalize.py` (add `--emit-field-only`)
- Test: `tests/integration/verify_lowmem_bitidentical.py` (leg 2)

**Interfaces:**
- Consumes: `reg_finalize.warp_source(src_slide, ws, dxdy) -> (pyvips.Image, reader)`; `reg_finalize.compose(...)`; `reg_finalize._save_ome_pyvips(warped, dst_f, channel_names, bf_dtype, tile_wh, compression)`.
- Produces:
  - `bin/reg_finalize.py --emit-field-only --out <slide_dxdy.v>` writes the composed padded field and exits without warping.
  - `bin/reg_warp_tile.py --warp-state W --field F --src-slide S --grid G.json --tile-idx I --out-dir D` writes `D/tile_<I>.v`.
  - `bin/reg_assemble.py --tiles-dir D --grid G.json --warp-state W --src-slide S --out O` writes the final OME-TIFF.
  - `grid.json` schema: `{"n_cols": int, "n_rows": int, "tile_wh": int, "width": int, "height": int, "tiles": [{"idx": int, "x": int, "y": int, "w": int, "h": int}, ...]}`.

- [ ] **Step 1: Add `--emit-field-only` to `reg_finalize.py`**

Add the argument next to the others:

```python
    ap.add_argument("--emit-field-only", action="store_true",
                    help="compose the padded displacement field, write it to --out, and exit "
                         "without warping (the warp is done by reg_warp_tile.py)")
```

Immediately after `slide_bk` is fully composed and before the `# 4) warp` block, insert:

```python
    if args.emit_field_only:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        _to_vips_field(slide_bk).write_to_file(args.out)
        print(f"[reg_finalize] wrote composed field -> {args.out}", flush=True)
        if heap_gb:
            registration.kill_jvm()
        return 0
```

No change to `_to_vips_field` is needed: `bin/reg_finalize.py:81-84` already returns a `pyvips.Image` unchanged and only converts the numpy `[dx, dy]` form. Verified during planning.

- [ ] **Step 2: Write the grid helper**

Add to `bin/utils/tile_grid.py`:

```python
def output_grid(width, height, tile_wh):
    """Partition an output canvas into a regular grid of non-overlapping tiles.

    No halo is needed: each tile is a .crop() of the SAME lazy warp, so pyvips pulls whatever
    source pixels the interpolator wants, including across tile edges.
    """
    tiles = []
    idx = 0
    for y in range(0, height, tile_wh):
        for x in range(0, width, tile_wh):
            tiles.append({"idx": idx, "x": x, "y": y,
                          "w": min(tile_wh, width - x), "h": min(tile_wh, height - y)})
            idx += 1
    return {"n_cols": (width + tile_wh - 1) // tile_wh,
            "n_rows": (height + tile_wh - 1) // tile_wh,
            "tile_wh": tile_wh, "width": width, "height": height, "tiles": tiles}
```

- [ ] **Step 3: Write `bin/reg_warp_tile.py`**

```python
#!/usr/bin/env python3
"""Warp ONE output tile of one slide (spec 5.3).

Bit-identical to the whole-image warp BY CONSTRUCTION: this runs the same lazy
reg_finalize.warp_source() and then .crop()s the requested region. pyvips is demand-driven,
so cropping the OUTPUT pulls exactly the source pixels that output region needs -- including
the bicubic kernel halo -- computed by the same code as the whole-image case. No halo maths,
no seam approximation.
"""
import argparse
import json
import os
import sys

import pyvips

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "utils"))

import reg_finalize


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--warp-state", required=True)
    ap.add_argument("--field", help="composed padded field from --emit-field-only; "
                                    "omit together with --rigid-only for the reference")
    ap.add_argument("--rigid-only", action="store_true",
                    help="no non-rigid field (dxdy=None), matching reg_finalize.py --rigid-only")
    ap.add_argument("--src-slide", required=True)
    ap.add_argument("--grid", required=True, help="grid.json from reg_assemble --write-grid")
    ap.add_argument("--tile-idx", type=int, required=True)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    if args.rigid_only == bool(args.field):
        raise SystemExit("pass exactly one of --field or --rigid-only")

    ws = json.load(open(args.warp_state))
    grid = json.load(open(args.grid))
    tile = next(t for t in grid["tiles"] if t["idx"] == args.tile_idx)

    # dxdy=None is the rigid-only path, byte-for-byte what reg_finalize.py --rigid-only does.
    # Do NOT substitute a zero field: warp_img takes different branches for None vs a supplied
    # field, and equivalence is not established.
    dxdy = None if args.rigid_only else pyvips.Image.new_from_file(args.field)
    warped, _ = reg_finalize.warp_source(args.src_slide, ws, dxdy)
    region = warped.crop(tile["x"], tile["y"], tile["w"], tile["h"])

    os.makedirs(args.out_dir, exist_ok=True)
    out = os.path.join(args.out_dir, f"tile_{args.tile_idx}.v")
    region.write_to_file(out)
    print(f"[reg_warp_tile] {out} ({tile['w']}x{tile['h']} @ {tile['x']},{tile['y']})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Write `bin/reg_assemble.py`**

```python
#!/usr/bin/env python3
"""Join warped output tiles into the final OME-TIFF (spec 5.3).

Two modes:
  --write-grid : compute the output canvas size from the warp state and emit grid.json
  (default)    : lazily open every tile_<i>.v, arrayjoin them, and write the OME-TIFF

Assembly is streaming: pyvips evaluates the arrayjoin tile-by-tile as tiffsave consumes it,
so peak RAM is O(one row of tiles), not O(slide).
"""
import argparse
import json
import os
import sys

import numpy as np
import pyvips

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "utils"))

import reg_finalize
from mirage_slide_reader import get_reader_for
from tile_grid import output_grid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--warp-state", required=True)
    ap.add_argument("--src-slide", required=True)
    ap.add_argument("--field", help="composed field; omit together with --rigid-only")
    ap.add_argument("--rigid-only", action="store_true",
                    help="no non-rigid field (dxdy=None); must match reg_warp_tile.py's flag")
    ap.add_argument("--write-grid", help="path to write grid.json, then exit")
    ap.add_argument("--tile-wh", type=int, default=4096)
    ap.add_argument("--tiles-dir")
    ap.add_argument("--grid")
    ap.add_argument("--out")
    ap.add_argument("--compression", default="lzw")
    args = ap.parse_args()

    ws = json.load(open(args.warp_state))

    if args.write_grid:
        if args.rigid_only == bool(args.field):
            raise SystemExit("pass exactly one of --field or --rigid-only")
        dxdy = None if args.rigid_only else pyvips.Image.new_from_file(args.field)
        warped, _ = reg_finalize.warp_source(args.src_slide, ws, dxdy)
        grid = output_grid(warped.width, warped.height, args.tile_wh)
        os.makedirs(os.path.dirname(os.path.abspath(args.write_grid)) or ".", exist_ok=True)
        json.dump(grid, open(args.write_grid, "w"))
        print(f"[reg_assemble] grid {grid['n_cols']}x{grid['n_rows']} "
              f"({len(grid['tiles'])} tiles) for {warped.width}x{warped.height}", flush=True)
        return 0

    grid = json.load(open(args.grid))
    rows = []
    for r in range(grid["n_rows"]):
        row = [pyvips.Image.new_from_file(os.path.join(args.tiles_dir, f"tile_{t['idx']}.v"))
               for t in sorted((t for t in grid["tiles"] if t["y"] == r * grid["tile_wh"]),
                               key=lambda t: t["x"])]
        rows.append(_join(row, "horizontal"))
    full = _join(rows, "vertical")

    reader = get_reader_for(args.src_slide, series=ws.get("series"))(
        args.src_slide, series=ws.get("series"))
    names = list(reader.metadata.channel_names or [f"C{i}" for i in range(full.bands)])
    if len(names) < full.bands:
        names += [f"C{i}" for i in range(len(names), full.bands)]
    tile_wh = min(reader.metadata.optimal_tile_wh, full.width, full.height)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    reg_finalize._save_ome_pyvips(full, args.out, names[:full.bands],
                                  _bf_dtype(full.format), tile_wh, args.compression)
    print(f"[reg_assemble] wrote {args.out} ({full.width}x{full.height} bands={full.bands})",
          flush=True)
    return 0


def _join(imgs, direction):
    out = imgs[0]
    for i in imgs[1:]:
        out = out.join(i, direction)
    return out


def _bf_dtype(vips_format):
    return {"uchar": "uint8", "char": "int8", "ushort": "uint16", "short": "int16",
            "uint": "uint32", "int": "int32", "float": "float", "double": "double"}[vips_format]


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Set the executable bit**

```bash
chmod +x bin/reg_warp_tile.py bin/reg_assemble.py
git update-index --chmod=+x bin/reg_warp_tile.py bin/reg_assemble.py
git ls-files -s bin/reg_warp_tile.py bin/reg_assemble.py
```
Expected: both lines start with `100755`.

- [ ] **Step 6: Add leg 2 to the integration test**

Append to `tests/integration/verify_lowmem_bitidentical.py` and call it from `main()`:

```python
def leg2(md):
    """Tile fan-out + assemble == the single-process warp."""
    ti = os.path.join(md, "tiler_inputs")
    ws = os.path.join(md, "warp_state.json")
    field_v = os.path.join(md, "slide_dxdy.v")
    src = os.path.join(INP, MOV)

    run([sys.executable, "bin/reg_finalize.py", "--inputs-dir", ti,
         "--field", os.path.join(md, "nr", "bk.v"), "--warp-state", ws,
         "--src-slide", src, "--out", field_v, "--emit-field-only"])

    grid_f = os.path.join(md, "grid.json")
    run([sys.executable, "bin/reg_assemble.py", "--warp-state", ws, "--src-slide", src,
         "--field", field_v, "--write-grid", grid_f, "--tile-wh", "64"])
    grid = json.load(open(grid_f))
    tiles_dir = os.path.join(md, "tiles_out")
    for t in grid["tiles"]:
        run([sys.executable, "bin/reg_warp_tile.py", "--warp-state", ws, "--field", field_v,
             "--src-slide", src, "--grid", grid_f, "--tile-idx", str(t["idx"]),
             "--out-dir", tiles_dir])
    tiled_out = os.path.join(md, "tiled.ome.tiff")
    run([sys.executable, "bin/reg_assemble.py", "--warp-state", ws, "--src-slide", src,
         "--grid", grid_f, "--tiles-dir", tiles_dir, "--out", tiled_out])

    a, b = px(os.path.join(md, "lazy.ome.tiff")), px(tiled_out)
    equal = a.shape == b.shape and np.array_equal(a, b)
    d = None if a.shape != b.shape else float(np.max(np.abs(a - b)))
    print("=" * 72)
    print(f"LEG 2 tile fan-out vs single warp: equal={equal} max|delta|={d} "
          f"({len(grid['tiles'])} tiles)")
    print("=" * 72, flush=True)
    return 0 if equal else 1


def main():
    md = setup()
    rc1 = leg1(md)
    rc2 = leg2(md)
    return rc1 or rc2
```

- [ ] **Step 7: Run both legs**

```bash
docker run --rm -v "$PWD":/work -w /work \
  bolt3x/attend_image_analysis:mirage_valis_1.0.0 \
  python3 tests/integration/verify_lowmem_bitidentical.py
```
Expected: `LEG 1 ... equal=True max|delta|=0.0` and `LEG 2 ... equal=True max|delta|=0.0` with more than one tile.

- [ ] **Step 8: Commit**

```bash
git rev-parse --abbrev-ref HEAD
git add bin/reg_warp_tile.py bin/reg_assemble.py bin/reg_finalize.py bin/utils/tile_grid.py \
        tests/integration/verify_lowmem_bitidentical.py
git commit -m ":sparkles: Split the full-res warp into independent output tiles"
```

---

### Task 5: Nextflow modules and adapter wiring

**Files:**
- Create: `modules/local/reg_warp_tile.nf`, `modules/local/reg_assemble.nf`
- Create: `tests/modules/reg_warp_tile.nf.test`, `tests/modules/reg_assemble.nf.test`
- Modify: `subworkflows/local/adapters/valis_distributed_adapter.nf`
- Modify: `nextflow.config`, `conf/modules.config`

**Interfaces:**
- Consumes: the three CLIs from Task 4.
- Produces: `REG_WARP_TILE.out.tile` = `tuple(pid, slide, path("tiles/tile_*.v"))`; `REG_ASSEMBLE.out.registered` = `tuple(pid, slide, path("registered_slides/*_registered.ome.tiff"))`, `REG_ASSEMBLE.out.versions`, `REG_ASSEMBLE.out.size_log`.

- [ ] **Step 1: Add the params**

In `nextflow.config`, after line 100:

```groovy
    reg_warp_tiles             = 1                     // 1 = single streaming warp task (low-resource default); >1 = fan out
    reg_warp_tile_wh           = 4096                  // output-tile edge for the warp fan-out
    reg_mem_budget_gb          = null                  // null = cluster defaults; set on a small machine to size tasks
    reg_compare                = false                 // run classic AND the new path, then diff them
```

- [ ] **Step 2: Write `modules/local/reg_warp_tile.nf`**

```groovy
process REG_WARP_TILE {
    tag "${patient_id}:${slide}:${tile_idx}"
    label 'process_low'

    container "${params.reg_dist_container ?: 'bolt3x/attend_image_analysis:mirage_valis_1.0.0'}"

    input:
    tuple val(patient_id), val(slide), path(warp_state), path(field), path(src_slide), path(grid), val(tile_idx)

    output:
    tuple val(patient_id), val(slide), path("tiles/tile_*.v"), emit: tile
    path "versions.yml",                                       emit: versions

    script:
    """
    reg_warp_tile.py \\
        --warp-state ${warp_state} \\
        --field ${field} \\
        --src-slide ${src_slide} \\
        --grid ${grid} \\
        --tile-idx ${tile_idx} \\
        --out-dir tiles

    cat <<-END_VERSIONS > versions.yml
    "\${task.process}":
        python: \$(python3 --version | sed 's/Python //')
        pyvips: \$(python3 -c "import pyvips; print(pyvips.__version__)")
    END_VERSIONS
    """

    stub:
    """
    mkdir -p tiles
    touch tiles/tile_${tile_idx}.v

    cat <<-END_VERSIONS > versions.yml
    "\${task.process}":
        python: stub
        pyvips: stub
    END_VERSIONS
    """
}
```

- [ ] **Step 3: Write `modules/local/reg_assemble.nf`**

```groovy
process REG_ASSEMBLE {
    tag "${patient_id}:${slide}"
    label 'process_medium'

    container "${params.reg_dist_container ?: 'bolt3x/attend_image_analysis:mirage_valis_1.0.0'}"

    input:
    tuple val(patient_id), val(slide), path(warp_state), path(src_slide), path(grid), path(tiles, stageAs: "tiles/*")

    output:
    tuple val(patient_id), val(slide), path("registered_slides/*_registered.ome.tiff"), emit: registered
    path "versions.yml",                                                                emit: versions
    path "size_log.txt",                                                                emit: size_log, optional: true

    script:
    def args = task.ext.args ?: ''
    """
    mkdir -p registered_slides
    reg_assemble.py \\
        --warp-state ${warp_state} \\
        --src-slide ${src_slide} \\
        --grid ${grid} \\
        --tiles-dir tiles \\
        --out registered_slides/${slide}_registered.ome.tiff \\
        ${args}

    du -sb registered_slides > size_log.txt || true

    cat <<-END_VERSIONS > versions.yml
    "\${task.process}":
        python: \$(python3 --version | sed 's/Python //')
        pyvips: \$(python3 -c "import pyvips; print(pyvips.__version__)")
    END_VERSIONS
    """

    stub:
    """
    mkdir -p registered_slides
    touch registered_slides/${slide}_registered.ome.tiff
    touch size_log.txt

    cat <<-END_VERSIONS > versions.yml
    "\${task.process}":
        python: stub
        pyvips: stub
    END_VERSIONS
    """
}
```

- [ ] **Step 4: Add resources and fix the mis-sizing**

In `conf/modules.config`, replace the block at line 260 (`withName: 'REG_NONRIGID|REG_MICRO_PREP' { publishDir = [ enabled: false ] }`) with:

```groovy
    // REG_NONRIGID runs DeepFlow on a 2-D image capped at params.reg_max_non_rigid_dim (4096) in a
    // JVM-free process -- single-digit GB of real work. It previously inherited process_medium
    // (100 GB + 100 GB/attempt), which negated the whole point of separating it out.
    withName: 'REG_NONRIGID' {
        cpus   = 4
        memory = { (params.reg_mem_budget_gb ? Math.min(16, params.reg_mem_budget_gb as int) : 16).GB * task.attempt }
        time   = { 4.h * task.attempt }
        publishDir = [ enabled: false ]
    }

    // REG_MICRO_PREP re-runs the rigid stage at micro resolution. With the lazy reader it no longer
    // needs a BioFormats heap, but it still holds the micro-resolution images.
    withName: 'REG_MICRO_PREP' {
        cpus   = 8
        memory = { (params.reg_mem_budget_gb ?: 64).GB * task.attempt }
        time   = { 12.h * task.attempt }
        publishDir = [ enabled: false ]
    }

    withName: 'REG_WARP_TILE' {
        cpus   = 2
        memory = { (params.reg_mem_budget_gb ? Math.min(8, params.reg_mem_budget_gb as int) : 8).GB * task.attempt }
        time   = { 4.h * task.attempt }
        maxForks = 50
        publishDir = [ enabled: false ]
    }

    withName: 'REG_ASSEMBLE' {
        cpus   = 4
        memory = { (params.reg_mem_budget_gb ?: 32).GB * task.attempt }
        time   = { 12.h * task.attempt }
        errorStrategy = {
            task.exitStatus in [1, 104, 134, 135, 137, 139, 140, 143] ? 'retry' : 'finish'
        }
        publishDir = [
            path: { "${params.outdir}/${patient_id}/registered" },
            mode: 'copy',
            pattern: "registered_slides/*_registered.ome.tiff",
            overwrite: true
        ]
        maxForks = 5
    }
```

**Selector warning:** Nextflow `withName` matches as a substring find, not a full match. `REG_NONRIGID` therefore also matches the alias `REG_NONRIGID_MICRO` — which is intended here, since both run the same script. Verified experimentally during design.

- [ ] **Step 5: Wire the fan-out into the adapter**

In `subworkflows/local/adapters/valis_distributed_adapter.nf`, add the includes:

```groovy
include { REG_WARP_TILE }  from '../../../modules/local/reg_warp_tile'
include { REG_ASSEMBLE }   from '../../../modules/local/reg_assemble'
```

Then define a reusable warp sub-flow at the bottom of the file:

```groovy
// Shared full-res warp: grid -> per-tile warp -> assemble. Every regime (tiled non-rigid,
// separated, separated+micro) AND the reference route through this, so the pipeline contains
// exactly ONE full-res warp implementation.
//
// KEY HAZARD (this file has been bitten three times already, see the comments above): patient_id
// arrives from the main workflow as a groupKey, but round-trips through process outputs as a plain
// String. A join key of [groupKey, slide] never matches [String, slide], and the symptom is not an
// error -- the downstream process simply stays pending forever. Every key below is .toString()'d.
workflow WARP_FANOUT {
    take:
    ch_in   // [pid, slide, warp_state, src_slide, field]  (field == [] means rigid-only)

    main:
    ch_norm = ch_in.map { pid, slide, ws, src, field ->
        tuple(pid.toString(), slide, ws, src, field)
    }

    REG_GRID(ch_norm)

    // Re-join the grid to its inputs, then emit one task per tile. The tile COUNT is read from
    // grid.json, which REG_GRID derived from the real warped canvas -- not from warp_state
    // arithmetic, which could disagree with what REG_WARP_TILE actually produces.
    ch_with_grid = ch_norm
        .map { pid, slide, ws, src, field -> tuple([pid, slide], ws, src, field) }
        .join(REG_GRID.out.grid.map { pid, slide, g -> tuple([pid.toString(), slide], g) })

    ch_tasks = ch_with_grid.flatMap { key, ws, src, field, grid ->
        def n = new groovy.json.JsonSlurper().parseText(grid.text).tiles.size()
        (0..<n).collect { i -> tuple(key[0], key[1], ws, src, field, grid, i) }
    }
    REG_WARP_TILE(ch_tasks)

    // Fan-in. groupTuple with no size waits for channel close, which is correct here: the tile
    // count per slide is only known after REG_GRID, so there is no size to supply up front.
    ch_tiles = REG_WARP_TILE.out.tile
        .map { pid, slide, t -> tuple([pid.toString(), slide], t) }
        .groupTuple()
        .map { key, tl -> tuple(key, tl.flatten()) }

    ch_assemble_in = ch_with_grid
        .map { key, ws, src, field, grid -> tuple(key, ws, src, grid) }
        .join(ch_tiles)
        .map { key, ws, src, grid, tiles -> tuple(key[0], key[1], ws, src, grid, tiles) }
    REG_ASSEMBLE(ch_assemble_in)

    emit:
    registered = REG_ASSEMBLE.out.registered
    versions   = REG_ASSEMBLE.out.versions
    size_log   = REG_ASSEMBLE.out.size_log
}
```

The three existing finalize processes stop warping and become **field emitters**. Rename them so the name matches what they now do, and delete `REG_WARP_REF` entirely — the reference now flows through the same grid/tile/assemble chain with `--rigid-only`, which also removes the 40 GB-heap process that has been OOMing on merged references.

| Before | After | Change |
|---|---|---|
| `modules/local/reg_finalize.nf` (`REG_FINALIZE`) | `modules/local/reg_compose_tiled.nf` (`REG_COMPOSE_TILED`) | stitches tiles → emits `slide_dxdy.v` |
| `modules/local/reg_finalize_field.nf` (`REG_FINALIZE_FIELD`) | `modules/local/reg_compose_field.nf` (`REG_COMPOSE_FIELD`) | emits `slide_dxdy.v` |
| `modules/local/reg_finalize_micro.nf` (`REG_FINALIZE_MICRO`) | `modules/local/reg_compose_micro.nf` (`REG_COMPOSE_MICRO`) | emits `slide_dxdy.v` |
| `modules/local/reg_warp_ref.nf` (`REG_WARP_REF`) | **deleted** | reference uses `REG_GRID`/`REG_WARP_TILE --rigid-only` |

For each renamed module apply exactly these three edits, shown here for `REG_COMPOSE_FIELD` (the other two are identical in shape — only the existing `reg_finalize.py` flags differ, and those stay unchanged):

```groovy
    output:
    tuple val(patient_id), val(slide), path("slide_dxdy.v"), emit: field
    path "versions.yml"                                    , emit: versions
    path "*.size.csv"                                      , emit: size_log
```

```groovy
    reg_finalize.py \\
        --inputs-dir tiler_inputs \\
        --field nr/bk.v \\
        --warp-state warp_state.json \\
        --src-slide ${src_slide} \\
        --emit-field-only \\
        --out slide_dxdy.v \\
        ${args}
```

and in the `stub:` block replace the `mkdir -p registered_slides; touch ...` lines with `touch slide_dxdy.v`.

`REG_COMPOSE_TILED` keeps its `--inputs-dir`/`--tiles-dir` arguments; `REG_COMPOSE_MICRO` keeps its `--micro-field`/`--micro-warp-state`/`--micro-inputs-dir` arguments. Only the output and the two added lines change.

Create `modules/local/reg_grid.nf`. Note `path(field)` with no `arity` accepts `[]` from the adapter and renders as an empty string — verified experimentally on Nextflow 25.04.7 — which is how the reference passes "no field":

```groovy
/*
 * REG_GRID - compute the output-tile grid for one slide's full-res warp.
 *
 * The canvas size is read from the ACTUAL lazily-warped pyvips image rather than derived from
 * warp_state arithmetic, so the grid can never disagree with what REG_WARP_TILE produces.
 * Cheap: nothing is evaluated, only the header geometry.
 */
process REG_GRID {
    tag "${patient_id}:${slide}"
    label 'process_low'

    container "${params.reg_dist_container ?: 'bolt3x/attend_image_analysis:mirage_valis_1.0.0'}"

    input:
    tuple val(patient_id), val(slide), path(warp_state, stageAs: 'warp_state.json'), path(src_slide, stageAs: 'src/*'), path(field)

    output:
    tuple val(patient_id), val(slide), path("grid.json"), emit: grid
    path "versions.yml",                                  emit: versions

    script:
    def field_arg = field ? "--field ${field}" : "--rigid-only"
    """
    reg_assemble.py \\
        --warp-state warp_state.json \\
        --src-slide ${src_slide} \\
        ${field_arg} \\
        --tile-wh ${params.reg_warp_tile_wh} \\
        --write-grid grid.json

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python3 --version | sed 's/Python //')
        pyvips: \$(python3 -c "import pyvips; print(pyvips.__version__)")
    END_VERSIONS
    """

    stub:
    """
    echo '{"n_cols":1,"n_rows":1,"tile_wh":4096,"width":8,"height":8,"tiles":[{"idx":0,"x":0,"y":0,"w":8,"h":8}]}' > grid.json

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        pyvips: stub
    END_VERSIONS
    """
}
```

Update `modules/local/reg_warp_tile.nf` (written in step 2) so its input carries the optional field and its script branches the same way:

```groovy
    input:
    tuple val(patient_id), val(slide), path(warp_state, stageAs: 'warp_state.json'), path(src_slide, stageAs: 'src/*'), path(field), path(grid, stageAs: 'grid.json'), val(tile_idx)

    script:
    def field_arg = field ? "--field ${field}" : "--rigid-only"
    """
    reg_warp_tile.py \\
        --warp-state warp_state.json \\
        --src-slide ${src_slide} \\
        ${field_arg} \\
        --grid grid.json \\
        --tile-idx ${tile_idx} \\
        --out-dir tiles
```

Update `modules/local/reg_assemble.nf`'s input to match the assemble CLI:

```groovy
    input:
    tuple val(patient_id), val(slide), path(warp_state, stageAs: 'warp_state.json'), path(src_slide, stageAs: 'src/*'), path(grid, stageAs: 'grid.json'), path(tiles, stageAs: "tiles/*")
```

- [ ] **Step 6: Rewire the adapter's call sites**

`WARP_FANOUT` must be invoked exactly **once** — a DSL2 workflow cannot be called twice without aliasing — so mix the moving slides and the reference into a single channel first.

In `subworkflows/local/adapters/valis_distributed_adapter.nf`, replace each regime's `REG_FINALIZE*` invocation with the matching `REG_COMPOSE_*`, assigning `ch_moving_field` instead of `ch_moving_registered`:

```groovy
    // regime (a)
    REG_COMPOSE_TILED(ch_finalize_in)
    ch_moving_field = REG_COMPOSE_TILED.out.field
    ch_moving_logs  = REG_COMPOSE_TILED.out.size_log

    // regime (b)
    REG_COMPOSE_FIELD(ch_finalize_in)
    ch_moving_field = REG_COMPOSE_FIELD.out.field
    ch_moving_logs  = REG_COMPOSE_FIELD.out.size_log

    // regime (c)
    REG_COMPOSE_MICRO(ch_fin_micro_in)
    ch_moving_field = REG_COMPOSE_MICRO.out.field
    ch_moving_logs  = REG_COMPOSE_MICRO.out.size_log
```

Then replace the whole `REG_WARP_REF` block and the `ch_registered` assembly at the end of the workflow with:

```groovy
    // ---- FULL-RES WARP (moving slides + the reference, one shared fan-out) ----
    // Keys normalised to String throughout: ch_prep_moving/ch_prep_ref/ch_src carry the groupKey
    // patient_id from the main workflow's streaming groupTuple, while process outputs carry a
    // plain String. Mixing the two in a join key silently never matches.
    ch_key_src = ch_src.map { key, meta, f -> tuple([key[0].toString(), key[1]], f) }
    ch_key_ws  = ch_prep_moving.map { key, ti, ws -> tuple([key[0].toString(), key[1]], ws) }

    ch_moving_warp = ch_moving_field
        .map { pid, slide, field -> tuple([pid.toString(), slide], field) }
        .join(ch_key_ws)
        .join(ch_key_src)
        .map { key, field, ws, src -> tuple(key[0], key[1], ws, src, field) }

    // The reference warps with its rigid M + crop only: field == [] selects --rigid-only, the
    // exact semantics the deleted REG_WARP_REF had. Classic VALIS warps every slide including the
    // reference, so downstream QC needs it in the same cropped coordinate space.
    ch_ref_warp = ch_prep_ref
        .map { key, ws -> tuple([key[0].toString(), key[1]], ws) }
        .join(ch_key_src)
        .map { key, ws, src -> tuple(key[0], key[1], ws, src, []) }

    WARP_FANOUT(ch_moving_warp.mix(ch_ref_warp))

    // ---- convert back to [meta, file] ----
    ch_registered = WARP_FANOUT.out.registered
        .map { pid, slide, regfile -> tuple([pid.toString(), slide], regfile) }
        .join(ch_src.map { key, meta, f -> tuple([key[0].toString(), key[1]], meta) })
        .map { key, regfile, meta -> tuple(meta, regfile) }

    emit:
    registered = ch_registered
    versions   = REG_PREP.out.versions.first()
    size_logs  = REG_PREP.out.size_log.mix(ch_moving_logs).mix(WARP_FANOUT.out.size_log)
```

Update the include block at the top of the file: drop `REG_FINALIZE`, `REG_FINALIZE_FIELD`, `REG_FINALIZE_MICRO`, `REG_WARP_REF`; add `REG_COMPOSE_TILED`, `REG_COMPOSE_FIELD`, `REG_COMPOSE_MICRO`, `REG_GRID`, `REG_WARP_TILE`, `REG_ASSEMBLE`.

Delete `modules/local/reg_warp_ref.nf` and `tests/modules/reg_finalize.nf.test`'s stale process references; rename the three existing `tests/modules/reg_finalize*.nf.test` files to match the new process names and update their `process` / `script` / `tag` lines and their output assertions (`process.out.field` instead of `process.out.registered`).

Also remove the now-dead `REG_WARP_REF` block from `conf/modules.config:264-289`, including its `ext.args = { "--jvm-heap-gb ..." }` — with the lazy reader there is no JVM heap to size.

- [ ] **Step 7: Write the stub nf-tests**

Create `tests/modules/reg_warp_tile.nf.test`:

```groovy
nextflow_process {
    name "Test process REG_WARP_TILE"
    script "modules/local/reg_warp_tile.nf"
    process "REG_WARP_TILE"
    tag "modules"; tag "modules_local"; tag "reg_warp_tile"

    test("stub - emits one tile") {
        options "-stub"
        when {
            process {
                """
                input[0] = Channel.of(tuple('P001', 'P001_mov1',
                    file('tests/testdata/P001_ref.ome.tiff'), file('tests/testdata/P001_ref.ome.tiff'),
                    file('tests/testdata/P001_mov1.ome.tiff'), file('tests/testdata/P001_ref.ome.tiff'), 0))
                """
            }
        }
        then {
            assert process.success
            assert process.out.tile.size() == 1
            assert process.out.versions.size() == 1
        }
    }
}
```

Create the analogous `tests/modules/reg_assemble.nf.test` for `REG_ASSEMBLE`, asserting `process.out.registered.size() == 1`.

- [ ] **Step 8: Run the stub tests**

```bash
nf-test test tests/modules/reg_warp_tile.nf.test tests/modules/reg_assemble.nf.test \
  --profile test,docker --verbose
```
Expected: 2 passed.

- [ ] **Step 9: Verify the default path is untouched**

```bash
nextflow run . -profile test,docker -stub --outdir /tmp/stubout
```
Expected: `EXIT: 0`, and no `REG_WARP_TILE` / `REG_ASSEMBLE` tasks in the output (the default is `reg_distributed_tiling=false`).

- [ ] **Step 10: Commit**

```bash
git rev-parse --abbrev-ref HEAD
git add -u modules/local/ tests/modules/
git add modules/local/reg_warp_tile.nf modules/local/reg_assemble.nf modules/local/reg_grid.nf \
        modules/local/reg_compose_tiled.nf modules/local/reg_compose_field.nf \
        modules/local/reg_compose_micro.nf \
        tests/modules/reg_warp_tile.nf.test tests/modules/reg_assemble.nf.test \
        subworkflows/local/adapters/valis_distributed_adapter.nf \
        conf/modules.config nextflow.config
git status --short   # confirm reg_warp_ref.nf shows as deleted and nothing unrelated is staged
git commit -m ":recycle: Split finalize into compose + grid + tile-warp + assemble"
```

---

### Task 6: `mode=add_cycle` support and the `reg_qc=2` gate

**Files:**
- Modify: `subworkflows/local/registration.nf:179-214`
- Modify: `subworkflows/local/add_cycle.nf:26, 68`
- Modify: `lib/ParamUtils.groovy`

**Interfaces:**
- Consumes: `VALIS_ADAPTER`, `VALIS_DISTRIBUTED_ADAPTER`.
- Produces: `ParamUtils.validateRegistrationPath(params)` — throws when `reg_qc >= 2` and `reg_distributed_tiling` is true.

- [ ] **Step 1: Add the validation**

In `lib/ParamUtils.groovy`:

```groovy
    /**
     * The distributed/low-memory registration path decomposes VALIS into separate processes and
     * therefore produces no single registrar pickle. reg_qc=2 (GeoJSON segmentation-overlap QC)
     * warps polygons THROUGH that pickle, so the two are mutually exclusive. Fail loudly at
     * launch rather than emitting an empty QC channel three hours in.
     */
    static void validateRegistrationPath(Map params) {
        def level = params.skip_registration_qc ? 0 : (params.reg_qc == null ? 1 : (params.reg_qc as int))
        if (params.reg_distributed_tiling && level >= 2) {
            throw new IllegalArgumentException(
                "reg_qc=${level} requires the classic VALIS registrar pickle, which the " +
                "distributed path does not produce. Use --reg_qc 1, or set " +
                "--reg_distributed_tiling false."
            )
        }
    }
```

Call it from wherever the other `ParamUtils` validators are invoked in `main.nf`.

- [ ] **Step 2: Extract the adapter selector**

In `subworkflows/local/registration.nf`, add above `workflow REGISTRATION`:

```groovy
// Single source of truth for "which registration adapter does this run use?", shared with
// add_cycle.nf so incremental cyclic-IF gets the same path as a full run.
def useDistributedAdapter() {
    return params.reg_distributed_tiling as boolean
}
```

- [ ] **Step 3: Use it in `add_cycle.nf`**

Add the include next to the existing `VALIS_ADAPTER` one at `subworkflows/local/add_cycle.nf:26`:

```groovy
include { VALIS_DISTRIBUTED_ADAPTER } from './adapters/valis_distributed_adapter'
```

Replace line 68 (`VALIS_ADAPTER(ch_grouped)`) and line 71 (`ch_new_registered = ...`) with:

```groovy
    // Same adapter choice as a full run (registration.nf), so add_cycle inherits the
    // low-memory path instead of being pinned to classic VALIS.
    if (useDistributedAdapter()) {
        VALIS_DISTRIBUTED_ADAPTER(ch_grouped)
        ch_adapter_registered = VALIS_DISTRIBUTED_ADAPTER.out.registered
        ch_adapter_versions   = VALIS_DISTRIBUTED_ADAPTER.out.versions
    } else {
        VALIS_ADAPTER(ch_grouped)
        ch_adapter_registered = VALIS_ADAPTER.out.registered
        ch_adapter_versions   = VALIS_ADAPTER.out.versions
    }

    // Keep only the newly registered cycle (drop the reference passthrough).
    ch_new_registered = ch_adapter_registered.filter { meta, _f -> !meta.is_reference }
```

`useDistributedAdapter()` is defined in `registration.nf` (Step 2); import it by moving the helper into `lib/ParamUtils.groovy` as a static method `ParamUtils.useDistributedAdapter(params)` and calling that from both files, so there is one definition rather than two.

The `reg_qc >= 2` block at lines 92-116 references `VALIS_ADAPTER.out.registrar`, which only exists on the classic branch. Guard it so it is unreachable on the distributed branch:

```groovy
    if (reg_qc_level >= 2 && !useDistributedAdapter()) {
```

Step 1's validator already rejects that combination at launch, so this guard is defence in depth rather than the primary gate — but without it the workflow would fail to compile on the distributed branch, because `VALIS_ADAPTER.out` is not defined there.

At line 250, replace `.mix(VALIS_ADAPTER.out.versions)` with `.mix(ch_adapter_versions)`.

- [ ] **Step 4: Verify with stubs**

```bash
nextflow run . -profile test,docker -stub --outdir /tmp/ac1 \
  --mode add_cycle --prior_outdir tests/output/prior --input tests/testdata/add_cycle.csv
nextflow run . -profile test,docker -stub --outdir /tmp/ac2 \
  --mode add_cycle --prior_outdir tests/output/prior --input tests/testdata/add_cycle.csv \
  --reg_distributed_tiling true
```
Expected: both `EXIT: 0`; the second shows `REG_PREP` / `REG_ASSEMBLE` tasks. If the fixture paths differ, take them from `tests/subworkflows/` and adjust.

- [ ] **Step 5: Verify the gate fires**

```bash
nextflow run . -profile test,docker -stub --outdir /tmp/gate \
  --reg_distributed_tiling true --reg_qc 2
```
Expected: fails at launch with the `reg_qc=2 requires the classic VALIS registrar pickle` message.

- [ ] **Step 6: Commit**

```bash
git rev-parse --abbrev-ref HEAD
git add lib/ParamUtils.groovy subworkflows/local/registration.nf subworkflows/local/add_cycle.nf
git commit -m ":sparkles: Support the distributed path in add_cycle; fast-fail reg_qc=2"
```

---

### Task 7: `--reg_compare`  — DONE

**Divergences from the code below, all forced by what landed after this plan was written:**
1. **Join key is `[patient_id, sorted channel signature]`, not `[patient_id, meta.id]`.** `meta.id` is
   optional on registration metas (the fixtures and the `--start registration` entry point omit
   it), so keying on it collapses every slide of a patient onto `[pid, null]`. `join` drops
   unmatched keys SILENTLY, so `COMPARE_REGISTRATION` would have run zero times and the run would
   still have been green — §4.4 vacuity, exactly. Added `failOnMismatch`/`failOnDuplicate` so a
   future key regression is an error rather than a no-op.
2. **Branches on `ParamUtils.regCompareEnabled(params)`**, not raw `params.reg_compare`
   (Task 6 introduced the coercion: a `-params-file` can deliver the string `"false"`, truthy in
   Groovy — here that silently doubles the cost of every run).
3. **`REG_COMPARE` also emits `registrar`**, and `validateRegistrationPath` returns early under
   `--reg_compare`: classic always runs on this branch, so `reg_qc=2` keeps working instead of
   emitting a silently empty seg-QC channel.
4. **The band-join moved to `bin/utils/vips_pages.py`** (pyvips only, no `valis`) and
   `mirage_slide_reader` re-exports it, rather than `compare_registration.py` carrying its own
   copy. §4.4(d)/(e) were both caused by page-reading logic getting duplicated and one copy
   getting it wrong.
5. **`REG_ASSEMBLE` publishes under `registered/candidate/` when `--reg_compare` is on.** Both
   paths otherwise publish into one `registered_slides/`; the names do not collide today, so the
   result would be a silently mixed directory while only classic's files are in the checkpoint CSV.
6. Added `tests/unit/test_compare_registration.py` (6 tests, in-image) and two paired nf-tests
   beyond the plan's stub run.

**Files:**
- Create: `bin/compare_registration.py` (mode 100755)
- Create: `bin/utils/vips_pages.py`
- Create: `modules/local/compare_registration.nf`
- Create: `subworkflows/local/reg_compare.nf`
- Create: `tests/unit/test_compare_registration.py`
- Modify: `subworkflows/local/registration.nf`, `lib/ParamUtils.groovy`, `conf/modules.config`,
  `bin/utils/mirage_slide_reader.py`, `tests/subworkflows/local/registration.nf.test`

**Interfaces:**
- Consumes: both adapters' `registered` channels.
- Produces: `COMPARE_REGISTRATION.out.metrics` = `tuple(meta, path("*_regcompare.json"))`, `.out.diff_png` = `tuple(meta, path("*_regdiff.png"))`.
- JSON schema: `{"slide": str, "shape": [h, w, c], "channels": [{"index": int, "name": str, "max_abs": float, "mean_abs": float, "rmse": float, "pct_differing": float}], "overall": {"max_abs": float, "mean_abs": float, "rmse": float, "pct_differing": float}}`.

- [ ] **Step 1: Write `bin/compare_registration.py`**

```python
#!/usr/bin/env python3
"""Compare two registered slides tile-by-tile and report how far apart they are.

Used by --reg_compare to answer "is the low-memory path close enough to classic VALIS that I
keep the VALIS guarantees?". Streams both images in tiles so it runs in bounded RAM on the same
low-resource machine the new path targets.
"""
import argparse
import json
import os

import numpy as np
import pyvips

TILE = 2048


def _bands(path):
    img = pyvips.Image.new_from_file(path, access="random", n=-1)
    try:
        ph = int(img.get("page-height"))
    except pyvips.Error:
        ph = img.height
    n = img.height // ph
    if n <= 1:
        return img
    b = [img.crop(0, i * ph, img.width, ph) for i in range(n)]
    return b[0].bandjoin(b[1:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True, help="classic registered slide")
    ap.add_argument("--b", required=True, help="new-path registered slide")
    ap.add_argument("--slide", required=True)
    ap.add_argument("--out-json", required=True)
    ap.add_argument("--out-png", required=True)
    ap.add_argument("--png-max-dim", type=int, default=2048)
    args = ap.parse_args()

    a, b = _bands(args.a), _bands(args.b)
    if (a.width, a.height, a.bands) != (b.width, b.height, b.bands):
        json.dump({"slide": args.slide, "error": "shape mismatch",
                   "a": [a.height, a.width, a.bands], "b": [b.height, b.width, b.bands]},
                  open(args.out_json, "w"), indent=2)
        raise SystemExit(f"shape mismatch: {a.width}x{a.height}x{a.bands} vs "
                         f"{b.width}x{b.height}x{b.bands}")

    c = a.bands
    max_abs = np.zeros(c)
    sum_abs = np.zeros(c)
    sum_sq = np.zeros(c)
    n_diff = np.zeros(c, dtype=np.int64)
    n_px = 0

    for y in range(0, a.height, TILE):
        h = min(TILE, a.height - y)
        for x in range(0, a.width, TILE):
            w = min(TILE, a.width - x)
            ta = _to_f64(a.crop(x, y, w, h), h, w, c)
            tb = _to_f64(b.crop(x, y, w, h), h, w, c)
            d = np.abs(ta - tb)
            max_abs = np.maximum(max_abs, d.reshape(-1, c).max(axis=0))
            sum_abs += d.reshape(-1, c).sum(axis=0)
            sum_sq += (d.reshape(-1, c) ** 2).sum(axis=0)
            n_diff += (d.reshape(-1, c) > 0).sum(axis=0)
            n_px += h * w

    names = _channel_names(args.a, c)
    chans = [{"index": i, "name": names[i], "max_abs": float(max_abs[i]),
              "mean_abs": float(sum_abs[i] / n_px), "rmse": float(np.sqrt(sum_sq[i] / n_px)),
              "pct_differing": float(100.0 * n_diff[i] / n_px)} for i in range(c)]
    report = {
        "slide": args.slide,
        "shape": [a.height, a.width, c],
        "channels": chans,
        "overall": {
            "max_abs": float(max_abs.max()),
            "mean_abs": float(sum_abs.sum() / (n_px * c)),
            "rmse": float(np.sqrt(sum_sq.sum() / (n_px * c))),
            "pct_differing": float(100.0 * n_diff.sum() / (n_px * c)),
        },
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out_json)) or ".", exist_ok=True)
    json.dump(report, open(args.out_json, "w"), indent=2)

    scale = min(1.0, args.png_max_dim / max(a.width, a.height))
    diff = (a.cast("float") - b.cast("float")).abs()
    if diff.bands > 1:
        diff = diff.bandmean()
    diff.resize(scale).cast("uchar").write_to_file(args.out_png)

    print(f"[compare_registration] {args.slide}: max|delta|={report['overall']['max_abs']} "
          f"pct_differing={report['overall']['pct_differing']:.6f}%", flush=True)
    return 0


def _to_f64(region, h, w, c):
    dt = {"uchar": np.uint8, "char": np.int8, "ushort": np.uint16, "short": np.int16,
          "uint": np.uint32, "int": np.int32, "float": np.float32, "double": np.float64}
    arr = np.frombuffer(region.write_to_memory(), dtype=dt[region.format])
    return arr.reshape(h, w, c).astype(np.float64)


def _channel_names(path, n):
    try:
        import tifffile, re
        with tifffile.TiffFile(path) as tf:
            xml = tf.ome_metadata or ""
        names = re.findall(r'<Channel[^>]*\bName="([^"]*)"', xml)
    except Exception:
        names = []
    return (list(names) + [f"C{i}" for i in range(len(names), n)])[:n]


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Set the executable bit**

```bash
chmod +x bin/compare_registration.py
git update-index --chmod=+x bin/compare_registration.py
git ls-files -s bin/compare_registration.py
```
Expected: `100755`.

- [ ] **Step 3: Write the module**

Create `modules/local/compare_registration.nf`:

```groovy
/*
 * COMPARE_REGISTRATION - diff the classic and low-memory registered slides for one image.
 *
 * Driven by --reg_compare. Streams both slides tile-by-tile so it runs in bounded RAM on the same
 * low-resource machine the new path targets.
 */
process COMPARE_REGISTRATION {
    tag "${meta.id}"
    label 'process_low'

    container "${params.reg_dist_container ?: 'bolt3x/attend_image_analysis:mirage_valis_1.0.0'}"

    input:
    tuple val(meta), path(classic, stageAs: 'classic/*'), path(candidate, stageAs: 'candidate/*')

    output:
    tuple val(meta), path("*_regcompare.json"), emit: metrics
    tuple val(meta), path("*_regdiff.png"),     emit: diff_png
    path "versions.yml",                        emit: versions

    script:
    def args = task.ext.args ?: ''
    """
    compare_registration.py \\
        --a ${classic} \\
        --b ${candidate} \\
        --slide ${meta.id} \\
        --out-json ${meta.id}_regcompare.json \\
        --out-png ${meta.id}_regdiff.png \\
        ${args}

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python3 --version | sed 's/Python //')
        pyvips: \$(python3 -c "import pyvips; print(pyvips.__version__)")
    END_VERSIONS
    """

    stub:
    """
    echo '{"slide":"${meta.id}","overall":{"max_abs":0.0,"mean_abs":0.0,"rmse":0.0,"pct_differing":0.0}}' > ${meta.id}_regcompare.json
    touch ${meta.id}_regdiff.png

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        pyvips: stub
    END_VERSIONS
    """
}
```

- [ ] **Step 4: Write the subworkflow**

Create `subworkflows/local/reg_compare.nf`:

```groovy
include { VALIS_ADAPTER             } from './adapters/valis_adapter'
include { VALIS_DISTRIBUTED_ADAPTER } from './adapters/valis_distributed_adapter'
include { COMPARE_REGISTRATION      } from '../../modules/local/compare_registration'

/*
 * Run BOTH registration paths over the SAME slides and report the difference. Opt-in via
 * --reg_compare; costs 2x registration. The classic output is the reference, the new path is
 * the candidate. This is how "are they close enough that I keep the VALIS guarantees?" gets an
 * answer on real data instead of a fixture.
 */
workflow REG_COMPARE {
    take:
    ch_grouped_multi

    main:
    VALIS_ADAPTER(ch_grouped_multi)
    VALIS_DISTRIBUTED_ADAPTER(ch_grouped_multi)

    ch_pairs = VALIS_ADAPTER.out.registered
        .map { meta, f -> tuple([meta.patient_id, meta.id], meta, f) }
        .join(VALIS_DISTRIBUTED_ADAPTER.out.registered
                .map { meta, f -> tuple([meta.patient_id, meta.id], f) })
        .map { key, meta, classic, candidate -> tuple(meta, classic, candidate) }

    COMPARE_REGISTRATION(ch_pairs)

    emit:
    registered = VALIS_ADAPTER.out.registered   // classic remains the run's real output
    metrics    = COMPARE_REGISTRATION.out.metrics
    diff_png   = COMPARE_REGISTRATION.out.diff_png
    versions   = COMPARE_REGISTRATION.out.versions.first()
}
```

- [ ] **Step 5: Branch on it in `registration.nf`**

Insert as the first branch of the adapter selection at `registration.nf:181`:

```groovy
    if (params.reg_compare) {
        REG_COMPARE(ch_grouped_multi)
        ch_registered       = REG_COMPARE.out.registered
        ch_adapter_logs     = Channel.empty()
        ch_adapter_versions = REG_COMPARE.out.versions
        ch_adapter_summary  = Channel.empty()
    } else if (!params.reg_distributed_tiling) {
```

Add the include and a `publishDir` entry in `conf/modules.config` putting `COMPARE_REGISTRATION` output under `${params.outdir}/${meta.patient_id}/registered/compare`.

- [ ] **Step 6: Verify with a stub run**

```bash
nextflow run . -profile test,docker -stub --outdir /tmp/cmp --reg_compare true
```
Expected: `EXIT: 0`, with `VALIS_ADAPTER`, `REG_PREP`, and `COMPARE_REGISTRATION` tasks all present.

- [ ] **Step 7: Commit**

```bash
git rev-parse --abbrev-ref HEAD
git add bin/compare_registration.py modules/local/compare_registration.nf \
        subworkflows/local/reg_compare.nf subworkflows/local/registration.nf conf/modules.config
git commit -m ":sparkles: Add --reg_compare: run both registration paths and diff them"
```

---

### Task 8: Stage timings and CI  — DONE

**Divergences from the code below:**
1. **The timings file is lifted to the TASK ROOT** (`<patient>_reg_prep_timings.json`), not left at
   `prep/stage_timings.json`. `publishDir`'s `pattern` cannot reach a file nested inside a
   directory output — `prep/` publishes as a unit or not at all — so the nested form ran green and
   published an EMPTY `timings/` directory. Caught by inspecting the published tree.
2. **A second CI job, `valis-unit`, runs the three in-image UNIT suites on every push/PR and is
   BLOCKING** (added to `all-tests`). Step 2 below only extends `distributed-integration`, which
   was `workflow_dispatch`-only — so following it literally would have left every guarantee still
   manual, which is the exact gap Task 8 exists to close.
3. `distributed-integration` now also triggers on push to main/dev (matching `nf-test-real`),
   not dispatch only.
4. Stage names are `load` / `rigid_and_prep` / `dump`; the timings write happens BEFORE the
   slide-loss guard, so a run that fails that guard still reports where its time went.

**Files:**
- Modify: `bin/reg_prep.py`
- Modify: `.github/workflows/ci.yml:346-369`

**Interfaces:**
- Consumes: everything above.
- Produces: `prep/<slide>/stage_timings.json` = `{"load": float, "rigid": float, "micro_rigid": float, "nonrigid_prep": float}` (seconds).

- [ ] **Step 1: Add stage timings to `reg_prep.py`**

Wrap the phases with a small helper and dump the result next to `warp_state.json`:

```python
import time

_TIMINGS = {}


class _stage:
    """Record wall-clock per prep phase, so the first real run says which loop to attack
    instead of us guessing. See spec section 5.6."""
    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        _TIMINGS[self.name] = round(time.perf_counter() - self.t0, 3)
        print(f"[reg_prep] stage {self.name}: {_TIMINGS[self.name]}s", flush=True)
        return False
```

Wrap the `Valis(...)` construction in `with _stage("load"):`, the `reg.register(...)` call in `with _stage("rigid_and_prep"):`, and the tiler-input dump in `with _stage("dump"):`. Write `json.dump(_TIMINGS, open(os.path.join(args.out, "stage_timings.json"), "w"))` before returning.

- [ ] **Step 2: Add the CI legs**

In `.github/workflows/ci.yml`, inside the `distributed-integration` job, after the existing verification step:

```yaml
      - name: Verify low-memory path is bit-identical
        run: |
          IMG="bolt3x/attend_image_analysis:mirage_valis_1.0.0"
          docker run --rm -v "$PWD":/work -w /work "$IMG" \
            python3 tests/integration/verify_lowmem_bitidentical.py

      - name: Probe reader equivalence (spec A1)
        run: |
          IMG="bolt3x/attend_image_analysis:mirage_valis_1.0.0"
          docker run --rm -v "$PWD":/work -w /work "$IMG" \
            python3 tests/integration/probe_reader_equivalence.py

      - name: Verify micro-registration is bit-identical
        run: |
          IMG="bolt3x/attend_image_analysis:mirage_valis_1.0.0"
          docker run --rm -v "$PWD":/work -w /work "$IMG" bash -lc '
            python3 tests/testdata/generate_large_fixture.py --size 1024 --out /tmp/bigdata &&
            python3 tests/integration/verify_micro_bitidentical.py'
```

The third step wires up `tests/integration/verify_micro_bitidentical.py`, which exists in the repo today but is referenced by no workflow.

- [ ] **Step 3: Run the full local suite**

```bash
pytest -v tests/ --ignore=tests/testdata --ignore=tests/modules \
  --ignore=tests/subworkflows --ignore=tests/integration
nf-test test --profile test,docker --verbose
nextflow run . -profile test,docker -stub --outdir /tmp/final
```
Expected: all green, `EXIT: 0`.

- [ ] **Step 4: Commit**

```bash
git rev-parse --abbrev-ref HEAD
git add bin/reg_prep.py .github/workflows/ci.yml
git commit -m ":wrench: Add REG_PREP stage timings and wire the bit-identical CI legs"
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §2 core finding / §3 guarantee ladder | 0, 2, 4 |
| §3 assumption A1 | 0 |
| §3 assumption A2 | 3 step 4 |
| §4 rejected rigid fan-out | not implemented, by design; §5.6 timings (Task 8) inform any revisit |
| §5.1 `mirage_slide_reader.py` | 1 |
| §5.2 injection points | 2, 3 |
| §5.3 warp split + `REG_WARP_REF` | 4, 5 |
| §5.4 `reg_mem_budget_gb` + `REG_NONRIGID` mis-sizing | 5 step 4 |
| §5.5 add_cycle + `reg_qc=2` gate | 6 |
| §5.6 stage timings | 8 |
| §7 `--reg_compare` | 7 |
| §8 testing table | 1, 2, 4, 5, 8 |
| §9 out of scope | respected — no `reg_qc=2` support, no brightfield work, BioFormats fallback retained in Task 1 |

**Known follow-ups deliberately left out of this plan** (YAGNI until measured): parallelising `serial_rigid.py:502-511` feature detection behind a seam patch, and pyramid-level support in the reader (mirage writes single-level TIFFs, so `slide2vips` correctly raises for `level != 0`).
