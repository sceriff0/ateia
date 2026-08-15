# Image I/O

<p class="standfirst">Every TIFF this pipeline writes, which of the four variants writes it, and the
one write the codebase does not control. Read this before adding a process that produces an
image, or before changing how an existing one is written.</p>

!!! abstract "Canonical sources"
    - **The writer** — `bin/utils/image_io.py` (module docstring carries the per-variant rationale)
    - **Machine-checked** — `tests/test_image_io_ownership.py` (static AST scan over all of `bin/`)
      and `tests/test_image_io_writer.py` (round-trips the bytes)
    - **Container → `tifffile` pin** — `docs/containers.md`

---

## bigtiff is mandatory, not an optimisation

`bin/tiled_stitch.py` wrote the rule, and `bin/utils/image_io.py` now applies it to everything:

> bigtiff is mandatory, not an optimisation: a registered slide is written uncompressed at full
> resolution, so classic TIFF's 32-bit offsets overflow
> (`struct.error: 'I' format requires 0 <= number <= 4294967295`) the moment the output crosses
> 4 GB — C × H × W × itemsize, reached by any real WSI.

That is why `bigtiff` is **not a parameter** of any entry point below. Before Task 11 it was a
per-call-site decision and five sites got it wrong the same way: the three segmentation backends
(`segment.py`, `segment_cellsam.py`, `segment_instantseg.py`) each wrote a full-resolution label
mask without it, `extract_mask_series.py` wrote uint32 masks with neither bigtiff nor compression,
and `bin/utils/qc.py` put bigtiff on its *downsampled* overlay while the *full-resolution*
composite in the same function went without.

---

## The four write variants

| Entry point | Used by | Why it is its own variant |
|---|---|---|
| `write_ome_tiff` | `convert_image.py`, `preprocess.py`, both registration-QC composites in `bin/utils/qc.py` | The default. Carries channel names and physical pixel size, which is what a consumer reading OME-XML expects. |
| `write_plain_tiff` | `split_multichannel.py` | Deliberately **not** OME. An OME header on a single-channel file would be a second, channel-less source of channel names for `extract_channel_names_from_ome` to find. Requires a stated `why_not_ome=` — the reason is an argument, so the variant stays reachable while the accident does not. |
| `write_mask_tiff` | `segment.py`, `segment_cellsam.py`, `segment_instantseg.py`, `extract_mask_series.py` | A label mask has no markers to name and every consumer indexes it positionally, so no OME header; and it is always compressed, because label images compress by an order of magnitude and the raw variant was the site that reached the 4 GB limit soonest. dtype is written through unchanged — label IDs are categorical and a silent cast renumbers cells. |
| `open_tiff_writer` | `tiled_stitch.py`, `merge_channels_pyramid.py` | The two cases that genuinely cannot be one call: a streamed tile generator (the slide is never materialised) and the true multi-resolution pyramid (base + `subifds` levels + an optional un-pyramided mask series). The caller keeps every decision about *what* is written; the wrapper owns only that the file is BigTIFF and that `ome` is stated rather than inferred from the filename. |

!!! note "The registration-QC composites open differently in ImageJ now"
    `*_QC_RGB_fullres.tif` used to be written with `imagej=True` and `mode: composite`, so Fiji
    opened it as a single RGB composite. It is OME now, like its downsampled sibling always was,
    so Fiji opens it via Bio-Formats as a 3-channel stack — the red/green overlay is still there,
    but you may need *Image ▸ Color ▸ Merge Channels* (or the Bio-Formats importer's "composite"
    view mode) to see it as one picture. Channel order is unchanged: 0 = registered (red),
    1 = reference (green), 2 = zero. Choosing one metadata convention was the point of the change;
    this is what it costs a person opening the file.

`merge_channels_pyramid.py` is the **only** real pyramid writer in the repo. `convert_image.py`
and `preprocess.py` write single-resolution OME-TIFFs; nothing else uses `subifds`. Its
`verify_ome_tiff()` runs after every write and is the check that the structure consumers read has
not moved.

---

## The write this codebase does not control

`bin/register.py` hands the entire registered-slide write to VALIS
(`slide_obj.warp_and_save_slide()`, pyvips-backed, not `tifffile`). bigtiff, compression, tile
shape and OME structure are all VALIS's choices; none of them are set or even visible from this
repo, and none of the guarantees on this page apply to that file. It is the largest artifact the
VALIS registration path produces.

This is deliberate and out of scope to change — changing it means either post-processing VALIS's
output or an upstream change to VALIS — but it is recorded here, and at the call site, so that the
one uncontrolled write is a known fact rather than a gap nobody wrote down.

!!! warning "TILED_STITCH's output is an OME-TIFF, despite an old comment saying otherwise"
    `tiled_stitch.py` writes `*_registered.ome.tiff`, and `tifffile` emits OME-XML for any
    filename ending `.ome.tif`/`.ome.tiff` — so that file has always carried an OME header, even
    though a comment beside the write claimed the header was deliberately omitted precisely to
    avoid a second, channel-less source of channel names. `extract_channel_names_from_ome` does
    read `['Channel:0:0', ...]` back out of it. It has not bitten because `SPLIT_CHANNELS` is
    passed `--channels` from `meta.channels` on every normal path, so the OME names are only a
    fallback that is not currently reached. Task 11 made the flag explicit (`ome=True`) to keep the
    artifact byte-for-byte identical and corrected the comment; **flipping it to non-OME changes a
    published artifact and is a separate decision.**

---

## Reads

The same file can be read eagerly or lazily, and the choice is now explicit at every site the
Task 11 survey listed:

| Site | Read |
|---|---|
| `segment.py` (`extract_dapi_channel`) | `tif.asarray(out="memmap")` — the proven pattern; only the nuclear channel is copied into RAM |
| `preprocess.py`, `split_multichannel.py`, `bin/utils/qc.py`, `extract_mask_series.py` | `tif.asarray(out="memmap")` — all four process or write one plane at a time, so none needs the whole stack resident |
| `tiled_coarse.py`, `tiled_reg_tile.py`, `tiled_stitch.py` | `tiled_io.open_lazy` — `tifffile.imread(aszarr=True)` + zarr, so a region read decodes only the tiles it touches |
| `export_spatialdata.py` (`read_mask`, `read_pyramid_lazy`) | `da.from_zarr` — SpatialData keeps the dask array, so materialising here would defeat the point of an out-of-core format |

A memory-mapped read of a *compressed* TIFF cannot map the file directly, so `tifffile` decodes
into a temporary file instead: the array stops costing RAM and starts costing scratch disk of the
same size. That is the trade every `out="memmap"` site above makes, and it is the one already made
by `segment.py`. Which sites actually pay it:

| Site | Input | Direct map, or `$TMPDIR` decode? |
|---|---|---|
| `preprocess.py` | `CONVERT_IMAGE.out.ome_tiff`, written `compression=None` | **direct map** — no temp file |
| `split_multichannel.py` | the registered slide | `TILED_STITCH`'s is uncompressed (direct); **VALIS's compression is VALIS's choice and invisible here**, so on the VALIS path, a decode |
| `bin/utils/qc.py` | the reference *and* the registered slide | likely **two** decodes in one task |
| `extract_mask_series.py` | `MERGE_AND_PYRAMID`'s pyramid, `--compression` defaulting to `zstd` | a decode, unless `--compression none` |

!!! warning "No `scratch` directive is set anywhere in this repo"
    `grep -rn scratch nextflow.config conf/` returns nothing, so Nextflow's default
    (`scratch = false`) applies: tasks run in their work directory, and `$TMPDIR` is whatever the
    executor's environment provides — on SLURM, typically a node-local `/tmp` or a per-job
    directory, **not** the work filesystem and **not** sized by the process's `memory` request.
    A `out="memmap"` decode of a compressed WSI therefore lands somewhere no `conf/modules.config`
    block accounts for. This is recorded, not fixed: setting `scratch` or a `TMPDIR` env is a
    resource decision for the site profile, and `conf/modules.config`'s one-owner rule governs
    what may be changed there. No site was observed failing on it — real (non-stub) runs do not
    execute on the development host.

Where a whole-array read is genuinely required, the site says so in a comment. The one deleted
outright was `bin/utils/tiled_io.py`'s `load_channels`: an eager whole-slide reader sitting next to
the lazy `open_lazy` that replaced it, with no caller left anywhere under `bin/`. Its behaviour
survives in `tests/test_tiled_reg_tile_lazy.py` as the oracle the lazy path is checked against,
which is the only use it had.

---

## Adding a process that writes an image

1. Import the entry point that matches the variant from `bin/utils/image_io.py`. Do **not** call
   `tifffile.imwrite` / `TiffWriter` / `imsave` / `memmap` directly —
   `tests/test_image_io_ownership.py` fails the build if you do, and names the file and line.
2. If none of the four variants fits, add a fifth **to `image_io.py`**, with the reason in its
   docstring. A fifth variant that lives in a script is how the pipeline ended up with six
   mutually distinct mechanisms and no owner.
3. `tifffile` is not the only library here that can write a TIFF — `bin/generate_preprocess_qc.py`
   imports `skimage.io.imsave` and `bin/utils/qc.py` calls `cv2.imwrite`. Both write PNG, which is
   fine and out of scope. But `test_image_io_ownership.py::test_foreign_image_writer_cannot_write_a_tiff`
   requires any such call's extension to be **statically visible** at the call site (a literal, an
   f-string, or `.with_suffix`) and non-TIFF; a path it cannot resolve fails just as a `.tif` does.
   PIL is closed by import: nothing under `bin/` imports it, and any `.save()` in a file that did
   would be flagged.
4. Check `docs/containers.md` for the `tifffile` version your process's image pins before using a
   feature that needs a newer one. The pins span `2023.2.28` to `2024.7.24`; `segmentation_gpu` is
   the floor, held there by `aicsimageio==4.14.0`'s `tifffile<2023.3.15` cap.
