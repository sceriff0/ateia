#!/usr/bin/env python3
"""Generate complete test fixtures for full pipeline testing.

Creates realistic test data including:
- Multi-channel OME-TIFF images (reference and moving)
- Segmentation masks
- CSV files for all pipeline entry points
- Both valid and invalid test cases for validation testing
"""

import json
from pathlib import Path

import numpy as np
import tifffile

# Deterministic seed — ensures identical test data across runs and CI environments
np.random.seed(42)

OUT_DIR = Path(__file__).parent
EXPECTED_DIR = OUT_DIR / "expected"
OUT_DIR.mkdir(exist_ok=True)
EXPECTED_DIR.mkdir(exist_ok=True)

print("Generating comprehensive test data (seed=42)...")

# =============================================================================
# 1. Generate realistic multi-channel OME-TIFF images
# =============================================================================

# Dedicated RNG for image anatomy so the shared structure is reproducible and
# independent of the global np.random stream used elsewhere in this script.
_img_rng = np.random.default_rng(42)


def make_anatomy(size, n_cells, rng):
    """Build a patient's shared tissue 'anatomy': a fixed set of cells
    (position, radius, base intensity) that every image of that patient renders.

    All of a patient's images (reference + moving) are drawn from this same
    anatomy, translated by a per-image ``shift``. That makes the DAPI channel a
    genuine geometric transform of the reference across slides, which is what
    VALIS feature-based registration needs to recover a transform. Without a
    shared structure, the "moving" images are independent random patterns and
    VALIS legitimately fails ("M is None"), which is why the real REGISTER
    nf-test was red. Cells live in [20, size-20] so a small shift never clips.
    """
    cells = []
    for _ in range(n_cells):
        cy = int(rng.integers(20, size[0] - 20))
        cx = int(rng.integers(20, size[1] - 20))
        # Nucleus-sized radius (diameter ~12-26 px): large enough for StarDist's
        # built-in 2D_versatile_fluo to detect them as nuclei, while staying
        # within bounds under the small per-image shift.
        radius = int(rng.integers(6, 13))
        intensity = int(rng.integers(8000, 15000))  # shared base, scaled per channel
        cells.append((cy, cx, radius, intensity))
    return cells


def _render_channel(anatomy, size, shift, scale, rng, add_noise=True):
    """Render one channel: draw the shared anatomy translated by ``shift`` with
    per-channel intensity ``scale`` (DAPI=1.0; markers dimmer/variable)."""
    img = np.zeros(size, dtype=np.uint16)
    yy, xx = np.ogrid[: size[0], : size[1]]
    for cy, cx, radius, intensity in anatomy:
        cy_s, cx_s = cy + shift[0], cx + shift[1]
        dist2 = (yy - cy_s) ** 2 + (xx - cx_s) ** 2
        circle_mask = dist2 <= radius**2
        gaussian = np.exp(-dist2 / (2 * (radius / 2) ** 2))
        img = np.maximum(
            img, (circle_mask * gaussian * intensity * scale).astype(np.uint16)
        )
    if add_noise:
        noise = rng.normal(100, 20, size).astype(np.int32)
        img = np.clip(img.astype(np.int32) + noise, 0, 65535).astype(np.uint16)
    return img


def create_multichannel_image(
    filename,
    anatomy,
    size=(128, 128),
    channel_names=None,
    add_noise=True,
    shift=(0, 0),
    rng=None,
    pixel_size_um=None,
):
    """Create a multi-channel OME-TIFF from a SHARED anatomy translated by ``shift``.

    Channel 0 (DAPI) is the registration reference channel: rendered at full
    intensity (scale 1.0) from the shared anatomy, it is near-identical across a
    patient's images up to the known ``shift`` (+ light noise), so VALIS can
    recover the transform. Marker channels reuse the same geometry at lower,
    channel-specific intensities (co-registered marker panels).

    ``pixel_size_um``, if given, stamps a real OME ``PhysicalSizeX``/``PhysicalSizeY``
    (in micrometres) onto the file. Every other fixture this generator writes omits
    it on purpose (see the `auto`-hard-fails-on-the-test-fixtures note in
    conf/test.config) -- this is the one knob that turns that on, for the shipped-
    defaults smoke fixture that needs `--pixel_size auto` to actually resolve.
    """
    if channel_names is None:
        channel_names = ["DAPI", "PANCK", "SMA"]
    if rng is None:
        rng = _img_rng
    channels = []
    for ch in range(len(channel_names)):
        scale = 1.0 if ch == 0 else float(rng.uniform(0.25, 0.7))
        channels.append(_render_channel(anatomy, size, shift, scale, rng, add_noise))

    # Stack channels (C, Y, X)
    multichannel = np.stack(channels, axis=0)

    metadata = {"axes": "CYX", "Channel": {"Name": channel_names}}
    if pixel_size_um is not None:
        metadata["PhysicalSizeX"] = pixel_size_um
        metadata["PhysicalSizeXUnit"] = "µm"
        metadata["PhysicalSizeY"] = pixel_size_um
        metadata["PhysicalSizeYUnit"] = "µm"

    # Save as OME-TIFF with proper metadata
    tifffile.imwrite(
        filename,
        multichannel,
        photometric="minisblack",
        metadata=metadata,
    )
    print(
        f"  Created {filename} - shape: {multichannel.shape}, channels: {channel_names}"
    )
    return multichannel


# Patient P001 - Reference + 2 moving images. All share ONE anatomy so the DAPI
# channel of each moving image is the reference DAPI translated by `shift`
# (true correspondence for VALIS registration). Each slide keeps its own marker
# panel. n_cells is generous so SuperPoint/SuperGlue has plenty of keypoints.
print("\n1. Creating multi-channel OME-TIFF images...")
p001_anatomy = make_anatomy((128, 128), n_cells=40, rng=_img_rng)
create_multichannel_image(
    OUT_DIR / "P001_ref.ome.tiff",
    p001_anatomy,
    channel_names=["DAPI", "PANCK", "SMA"],
    shift=(0, 0),
    rng=_img_rng,
)
create_multichannel_image(
    OUT_DIR / "P001_mov1.ome.tiff",
    p001_anatomy,
    channel_names=["DAPI", "CD3", "CD8"],
    shift=(5, 5),
    rng=_img_rng,
)
create_multichannel_image(
    OUT_DIR / "P001_mov2.ome.tiff",
    p001_anatomy,
    channel_names=["DAPI", "VIMENTIN", "CD45"],
    shift=(-3, 4),
    rng=_img_rng,
)

# Patient P002 - Single slide (reference only); its own shared anatomy.
p002_anatomy = make_anatomy((128, 128), n_cells=40, rng=_img_rng)
create_multichannel_image(
    OUT_DIR / "P002_ref.ome.tiff",
    p002_anatomy,
    channel_names=["DAPI", "PANCK", "SMA"],
    shift=(0, 0),
    rng=_img_rng,
)

# Keep-set regression images. Both groups use a DEDICATED rng so they can be added
# without shifting the _img_rng stream that renders the fixtures above (their content
# is asserted elsewhere).
_keepset_rng = np.random.default_rng(43)

# (a) A single-channel DAPI-only moving slide. Paired with P001_ref (DAPI|PANCK|SMA)
#     in keepset_exhausted.csv, its ONE channel is already claimed by the reference, so
#     CsvUtils.resolveKeptChannelsPerSlide resolves its keep-set to the EMPTY list.
create_multichannel_image(
    OUT_DIR / "P001_mov_dapi_only.ome.tiff",
    p001_anatomy,
    channel_names=["DAPI"],
    shift=(5, 5),
    rng=_keepset_rng,
)

# (b) Two slides of ONE patient sharing a basename under different directories -- the
#     ordinary cyclic-IF layout of one directory per cycle. resolveKeptChannelsPerSlide
#     keys its per-slide map on the RAW samplesheet cell for exactly this shape; keyed on
#     the basename these two rows overwrote each other.
for _cycle_dir, _channels in (("cycle1", ["DAPI", "CD3"]), ("cycle2", ["DAPI", "CD8"])):
    (OUT_DIR / _cycle_dir).mkdir(exist_ok=True)
    create_multichannel_image(
        OUT_DIR / _cycle_dir / "slide.ome.tiff",
        p001_anatomy,
        channel_names=_channels,
        shift=(0, 0) if _cycle_dir == "cycle1" else (5, 5),
        rng=_keepset_rng,
    )

# =============================================================================
# 2. Generate segmentation masks
# =============================================================================
print("\n2. Creating segmentation masks...")


def create_segmentation_mask(filename, size=(128, 128), n_cells=15):
    """Create a realistic segmentation mask with labeled cells."""
    mask = np.zeros(size, dtype=np.int32)

    label = 1
    attempts = 0
    max_attempts = n_cells * 10

    while label <= n_cells and attempts < max_attempts:
        attempts += 1

        # Random cell center
        cy = np.random.randint(10, size[0] - 10)
        cx = np.random.randint(10, size[1] - 10)
        radius = np.random.randint(4, 9)

        # Check for overlap
        yy, xx = np.ogrid[: size[0], : size[1]]
        circle_mask = (yy - cy) ** 2 + (xx - cx) ** 2 <= radius**2

        if mask[circle_mask].sum() == 0:  # No overlap
            mask[circle_mask] = label
            label += 1

    np.save(filename, mask)
    # Production segmentation masks come from SEGMENT as uint32 TIFFs (segment.py
    # writes *_cell_mask.tif with compression='zlib'). Modules that read the mask
    # as an image (e.g. MERGE_AND_PYRAMID) need a TIFF, not the .npy fixture, so
    # write a matching .tif alongside it.
    tif_path = Path(filename).with_suffix(".tif")
    tifffile.imwrite(tif_path, mask.astype(np.uint32), compression="zlib")
    print(f"  Created {filename} and {tif_path.name} - {label - 1} cells")
    return mask


p001_cell_mask = create_segmentation_mask(OUT_DIR / "P001_cell_mask.npy", n_cells=20)
create_segmentation_mask(OUT_DIR / "P002_cell_mask.npy", n_cells=15)


def create_nuclei_mask(cell_mask, filename, shrink=0.6):
    """Write a nuclei label mask NESTED inside ``cell_mask``'s labels.

    Production nuclei masks come from SEGMENT as uint32 TIFFs whose label IDs are
    the SAME IDs as the cell mask's -- EXTRACT_NUCLEI_PROPERTIES re-keys nucleus
    contours onto the cell label and every downstream join is an identity on it.
    So this derives the nuclei from the cell mask rather than drawing a second,
    independent set of blobs: for each label, keep the pixels within
    ``shrink`` x the label's equivalent radius of its centroid.

    NO RANDOM DRAWS. Every other fixture in this file is seeded off the module-level
    np.random stream (or one of the two dedicated Generators), and inserting a draw
    here would shift every fixture written after it -- a whole-tree content change
    for a file that only needed to exist. This function is pure geometry over an
    array that has already been written.
    """
    nuclei = np.zeros(cell_mask.shape, dtype=np.uint32)
    yy, xx = np.indices(cell_mask.shape)
    for label in np.unique(cell_mask):
        if label == 0:
            continue
        sel = cell_mask == label
        cy, cx = yy[sel].mean(), xx[sel].mean()
        radius = np.sqrt(sel.sum() / np.pi) * shrink
        nuclei[sel & (((yy - cy) ** 2 + (xx - cx) ** 2) <= radius**2)] = label
    tifffile.imwrite(filename, nuclei, compression="zlib")
    print(f"  Created {Path(filename).name} - {len(np.unique(nuclei)) - 1} nuclei")
    return nuclei


create_nuclei_mask(p001_cell_mask, OUT_DIR / "P001_nuclei_mask.tif")

# =============================================================================
# 3. Generate valid input CSVs for each pipeline entry point
# =============================================================================
print("\n3. Creating valid input CSVs...")

# Use absolute paths resolved from the output directory
# This ensures CSVs work regardless of where the pipeline is launched from
TESTDATA_ABS = str(OUT_DIR.resolve())

# 3a. Valid input for preprocessing step (ND2 conversion disabled)
with open(OUT_DIR / "valid_preprocessing.csv", "w") as f:
    f.write("patient_id,path_to_file,is_reference,channels\n")
    f.write(f"P001,{TESTDATA_ABS}/P001_ref.ome.tiff,true,DAPI|PANCK|SMA\n")
    f.write(f"P001,{TESTDATA_ABS}/P001_mov1.ome.tiff,false,DAPI|CD3|CD8\n")
    f.write(f"P001,{TESTDATA_ABS}/P001_mov2.ome.tiff,false,DAPI|VIMENTIN|CD45\n")
    f.write(f"P002,{TESTDATA_ABS}/P002_ref.ome.tiff,true,DAPI|PANCK|SMA\n")
print("  Created valid_preprocessing.csv")

# 3b. Valid checkpoint CSV for registration step. Deliberately WITHOUT an 'id'
#     column (RULING R17, lib/Checkpoint.groovy -- a real registered.csv now
#     carries one): --start registration/segmentation both read a checkpoint
#     through INPUT_CHECK's samplesheet-shaped reader (Meta.fromSamplesheetRow),
#     which derives id itself from the entry image column and never reads a
#     persisted 'id' value. Keeping this fixture id-less is what proves that
#     path stays backward-compatible with an OLDER checkpoint file.
with open(OUT_DIR / "valid_checkpoint_registration.csv", "w") as f:
    f.write("patient_id,preprocessed_image,is_reference,channels\n")
    f.write(f"P001,{TESTDATA_ABS}/P001_ref.ome.tiff,true,DAPI|PANCK|SMA\n")
    f.write(f"P001,{TESTDATA_ABS}/P001_mov1.ome.tiff,false,DAPI|CD3|CD8\n")
print("  Created valid_checkpoint_registration.csv")

# 3c. Valid checkpoint CSV for postprocessing step (used as a --start segmentation
#     input in tests/checkpoint_manifest.nf.test, also via INPUT_CHECK -- same
#     id-less-is-fine reasoning as 3b above).
with open(OUT_DIR / "valid_checkpoint_postprocessing.csv", "w") as f:
    f.write("patient_id,registered_image,is_reference,channels\n")
    f.write(f"P001,{TESTDATA_ABS}/P001_ref.ome.tiff,true,DAPI|PANCK|SMA\n")
print("  Created valid_checkpoint_postprocessing.csv")

# 3c-bis. Valid checkpoint CSVs for the segmentation step (tests/subworkflows/local/
#         segmentation.nf.test's READ_SEGMENTED_CHECKPOINT tests). Same TESTDATA_ABS
#         convention as every other valid_checkpoint_*.csv above: absolute paths
#         resolved at generation time, so the fixture is rooted at whatever checkout
#         is running it, not a hardcoded worktree path. P001_nuclei_mask.tif is the
#         hand-authored fixture the generator does not write (see .gitignore's
#         comment on this file); P001_cell_mask.tif and sample_contours.json are
#         both written elsewhere in this script.
# id (RULING R17, lib/Checkpoint.groovy) IS required here: READ_SEGMENTED_CHECKPOINT
# builds meta through Meta.fromCheckpointRow, which throws on a row with no id.
# pixel_size (this task) is required the same way -- appended last, 0.325 to match
# conf/test.config's pin.
with open(OUT_DIR / "valid_checkpoint_segmented.csv", "w") as f:
    f.write("patient_id,id,registered_image,is_reference,channels,cell_mask,nuclei_mask,contours,nucleus_contours,pixel_size\n")
    f.write(
        f"P001,P001_ref,{TESTDATA_ABS}/P001_ref.ome.tiff,true,DAPI|PANCK|SMA,"
        f"{TESTDATA_ABS}/P001_cell_mask.tif,{TESTDATA_ABS}/P001_nuclei_mask.tif,"
        f"{TESTDATA_ABS}/sample_contours.json,{TESTDATA_ABS}/sample_contours.json,0.325\n"
    )
    f.write(
        f"P001,P001_mov1,{TESTDATA_ABS}/P001_mov1.ome.tiff,false,DAPI|CD3|CD8,"
        f"{TESTDATA_ABS}/P001_cell_mask.tif,{TESTDATA_ABS}/P001_nuclei_mask.tif,"
        f"{TESTDATA_ABS}/sample_contours.json,{TESTDATA_ABS}/sample_contours.json,0.325\n"
    )
print("  Created valid_checkpoint_segmented.csv")

# nuclei_mask populated, nucleus_contours empty -- the shape a checkpoint written
# under --quantify_compartments false actually has (EXTRACT_NUCLEI_PROPERTIES never
# ran, but SEGMENT always produces nuclei_mask regardless of that flag).
with open(OUT_DIR / "valid_checkpoint_segmented_no_compartments.csv", "w") as f:
    f.write("patient_id,id,registered_image,is_reference,channels,cell_mask,nuclei_mask,contours,nucleus_contours,pixel_size\n")
    f.write(
        f"P001,P001_ref,{TESTDATA_ABS}/P001_ref.ome.tiff,true,DAPI|PANCK|SMA,"
        f"{TESTDATA_ABS}/P001_cell_mask.tif,{TESTDATA_ABS}/P001_nuclei_mask.tif,"
        f"{TESTDATA_ABS}/sample_contours.json,,0.325\n"
    )
print("  Created valid_checkpoint_segmented_no_compartments.csv")

# 3c-ter. tests/subworkflows/entry_point_equivalence.nf.test's fixture -- the ONE
#         permanent, CI-collected guard that a checkpoint-entered meta
#         (READ_SEGMENTED_CHECKPOINT -> Meta.fromCheckpointRow) carries
#         keep_channels/channels_count, not just patient_id/id/is_reference/channels.
#         Column list from lib/Checkpoint.groovy's 'segmented' entry (authoritative;
#         read it, never restate it by hand) -- it now includes 'id' (RULING R17).
#
#         Channel declarations are DELIBERATELY IDENTICAL to test_input.csv's two
#         rows (P001 ref DAPI|PANCK|SMA, P001 mov1 DAPI|CD3|CD8) so the expected
#         channels_count (5) is the SAME value INPUT_CHECK's
#         CsvUtils.countChannelsPerPatient already computes for that exact
#         declared-channel structure on the samplesheet path -- see
#         tests/subworkflows/local/input_check.nf.test's
#         `workflow.out.counts[0].channels.P001 == 5` assertion against
#         test_input.csv. That is the cross-check the new nf-test's comment points
#         at: the checkpoint path's channels_count must equal the samplesheet
#         path's for the same declared channels, and this fixture makes that
#         equality checkable without needing CsvUtils on the nf-test assertion
#         classpath (which does not have lib/ available -- see tests/layout.nf.test's
#         header comment).
with open(OUT_DIR / "segmented.csv", "w") as f:
    f.write("patient_id,id,registered_image,is_reference,channels,cell_mask,nuclei_mask,contours,nucleus_contours,pixel_size\n")
    f.write(
        f"P001,P001_ref,{TESTDATA_ABS}/P001_ref.ome.tiff,true,DAPI|PANCK|SMA,"
        f"{TESTDATA_ABS}/P001_cell_mask.tif,{TESTDATA_ABS}/P001_nuclei_mask.tif,"
        f"{TESTDATA_ABS}/sample_contours.json,{TESTDATA_ABS}/sample_contours.json,0.325\n"
    )
    f.write(
        f"P001,P001_mov1,{TESTDATA_ABS}/P001_mov1.ome.tiff,false,DAPI|CD3|CD8,"
        f"{TESTDATA_ABS}/P001_cell_mask.tif,{TESTDATA_ABS}/P001_nuclei_mask.tif,"
        f"{TESTDATA_ABS}/sample_contours.json,{TESTDATA_ABS}/sample_contours.json,0.325\n"
    )
print("  Created segmented.csv (entry_point_equivalence.nf.test fixture)")

# 3d. A minimal "prior completed run" for the add_cycle path. ADD_CYCLE rebuilds
#     the assets it reuses from these two checkpoint CSVs under
#     <prior_outdir>/csv/, so tests/subworkflows/add_cycle.nf.test only has to
#     point --prior_outdir at this directory. Every referenced file must really
#     exist: Nextflow stages merged_csv and pyramid into the processes.
#     add_cycle.nf's own readers don't dereference the 'id' column (they extract
#     specific named columns into synthetic per-patient assets, never a per-image
#     meta), but it's included anyway to match what a REAL registered.csv/
#     postprocessed.csv now always carries (RULING R17).
PRIOR_DIR = OUT_DIR / "prior_run" / "csv"
PRIOR_DIR.mkdir(parents=True, exist_ok=True)
with open(PRIOR_DIR / "registered.csv", "w") as f:
    f.write("patient_id,id,registered_image,is_reference,channels\n")
    f.write(f"P001,P001_image,{TESTDATA_ABS}/P001_image.tiff,true,DAPI|PANCK\n")
with open(PRIOR_DIR / "postprocessed.csv", "w") as f:
    f.write("patient_id,id,cell_csv,cell_geojson,merged_csv,cell_mask,pyramid\n")
    f.write(
        f"P001,P001,{TESTDATA_ABS}/P001_merged_quant.csv,{TESTDATA_ABS}/sample_contours.json,"
        f"{TESTDATA_ABS}/P001_merged_quant.csv,{TESTDATA_ABS}/P001_cell_mask.tif,"
        f"{TESTDATA_ABS}/P001_pyramid.ome.tiff\n"
    )
print("  Created prior_run/csv/{registered,postprocessed}.csv")

# 3d-bis. The prior run's two IMAGE fixtures, named by the two checkpoint CSVs above.
#
#   P001_image.tiff       -- the prior run's registered reference. registered.csv
#                            declares it DAPI|PANCK, so it carries exactly those two.
#   P001_pyramid.ome.tiff -- the prior run's combined pyramid, WITH the mask series.
#                            bin/extract_mask_series.py exits non-zero unless series 1
#                            is a (2, H, W) unsigned-integer [cell, nuclei] stack, and
#                            add_cycle.nf reads the prior channel names off series 0.
#
# A DEDICATED Generator, like the keep-set fixtures above: drawing from _img_rng here
# would shift the stream that renders every image written before this point.
#
# Written with an explicit `ome=True` / TiffWriter rather than through
# create_multichannel_image, because tifffile only emits OME-XML by default for a
# name ending in `.ome.tif`/`.ome.tiff` -- and the first of these two deliberately
# does not (the checkpoint CSV names it `P001_image.tiff`).
_prior_rng = np.random.default_rng(44)
_prior_channels = ["DAPI", "PANCK"]
_prior_planes = np.stack(
    [
        _render_channel(p001_anatomy, (128, 128), (0, 0), 1.0 if ch == 0 else 0.5, _prior_rng)
        for ch in range(len(_prior_channels))
    ]
)
tifffile.imwrite(
    OUT_DIR / "P001_image.tiff",
    _prior_planes,
    photometric="minisblack",
    ome=True,
    metadata={"axes": "CYX", "Channel": {"Name": _prior_channels}},
)
print(f"  Created P001_image.tiff - shape: {_prior_planes.shape}, channels: {_prior_channels}")

_prior_masks = np.stack(
    [
        tifffile.imread(OUT_DIR / "P001_cell_mask.tif").astype(np.uint32),
        tifffile.imread(OUT_DIR / "P001_nuclei_mask.tif").astype(np.uint32),
    ]
)
with tifffile.TiffWriter(OUT_DIR / "P001_pyramid.ome.tiff", ome=True, bigtiff=True) as _tif:
    # subifds=1 reserves one sub-resolution level, exactly as
    # bin/merge_channels_pyramid.py does; the next write with subfiletype=1 fills it.
    _tif.write(
        _prior_planes,
        photometric="minisblack",
        subifds=1,
        metadata={"axes": "CYX", "Channel": {"Name": _prior_channels}},
    )
    _tif.write(_prior_planes[:, ::2, ::2], photometric="minisblack", subfiletype=1)
    # A separate top-level write with its own metadata becomes OME Image:1.
    _tif.write(
        _prior_masks,
        photometric="minisblack",
        metadata={"axes": "CYX", "Channel": {"Name": ["cell_mask", "nuclei_mask"]}},
    )
print(f"  Created P001_pyramid.ome.tiff - 2 series (image {_prior_planes.shape} + masks {_prior_masks.shape})")

# 3e. The NEW-CYCLE samplesheet that goes with prior_run/ — i.e. what a real
#     `--mode add_cycle --prior_outdir <prior_run> --input <this>` run consumes.
#     By design it has NO reference row: the registration reference is the frozen
#     prior-run reference, which is never a row in this sheet (mirage.nf passes
#     allow-no-reference=true for exactly this shape).
#
#     ONE slide, deliberately. add_cycle builds one registration group per new
#     slide, each carrying the prior reference, so N new slides for a patient
#     re-emit that reference N times — which would put N identical reference rows
#     in csv/registered.csv. Keeping the fixture at one slide keeps the manifest
#     test asserting the writer rather than that pre-existing fan-out.
with open(OUT_DIR / "new_cycle.csv", "w") as f:
    f.write("patient_id,path_to_file,is_reference,channels\n")
    f.write(f"P001,{TESTDATA_ABS}/P001_mov1.ome.tiff,false,DAPI|CD3|CD8\n")
print("  Created new_cycle.csv (add_cycle new-cycle samplesheet for prior_run/)")

# =============================================================================
# 4. Generate INVALID input CSVs for validation testing
# =============================================================================
print("\n4. Creating invalid input CSVs for validation tests...")

# 4a. Multiple references per patient
with open(OUT_DIR / "invalid_multi_ref.csv", "w") as f:
    f.write("patient_id,path_to_file,is_reference,channels\n")
    f.write(f"P001,{TESTDATA_ABS}/P001_ref.ome.tiff,true,DAPI|PANCK|SMA\n")
    f.write(
        f"P001,{TESTDATA_ABS}/P001_mov1.ome.tiff,true,DAPI|CD3|CD8\n"
    )  # SECOND REF!
    f.write(f"P001,{TESTDATA_ABS}/P001_mov2.ome.tiff,false,DAPI|VIMENTIN|CD45\n")
print("  Created invalid_multi_ref.csv (multiple references)")

# 4b. No reference per patient
with open(OUT_DIR / "invalid_no_ref.csv", "w") as f:
    f.write("patient_id,path_to_file,is_reference,channels\n")
    f.write(f"P001,{TESTDATA_ABS}/P001_mov1.ome.tiff,false,DAPI|CD3|CD8\n")
    f.write(f"P001,{TESTDATA_ABS}/P001_mov2.ome.tiff,false,DAPI|VIMENTIN|CD45\n")
print("  Created invalid_no_ref.csv (no reference)")

# 4c. DAPI not in channel 0 (pre-converted OME-TIFF input)
with open(OUT_DIR / "invalid_dapi_position.csv", "w") as f:
    f.write("patient_id,path_to_file,is_reference,channels\n")
    f.write(
        f"P001,{TESTDATA_ABS}/P001_ref.ome.tiff,true,PANCK|DAPI|SMA\n"
    )  # DAPI NOT FIRST
print("  Created invalid_dapi_position.csv (DAPI not in position 0)")

# 4d. Missing DAPI channel
with open(OUT_DIR / "invalid_no_dapi.csv", "w") as f:
    f.write("patient_id,path_to_file,is_reference,channels\n")
    f.write(f"P001,{TESTDATA_ABS}/P001_ref.ome.tiff,true,PANCK|SMA\n")  # NO DAPI
print("  Created invalid_no_dapi.csv (missing DAPI)")

# 4e. Invalid checkpoint - missing required column
with open(OUT_DIR / "invalid_checkpoint_missing_col.csv", "w") as f:
    f.write("patient_id,preprocessed_image,is_reference\n")  # Missing 'channels'
    f.write(f"P001,{TESTDATA_ABS}/P001_ref.ome.tiff,true\n")
print("  Created invalid_checkpoint_missing_col.csv (missing column)")

# 4f. Invalid checkpoint - malformed is_reference
with open(OUT_DIR / "invalid_checkpoint_bad_ref.csv", "w") as f:
    f.write("patient_id,preprocessed_image,is_reference,channels\n")
    f.write(
        f"P001,{TESTDATA_ABS}/P001_ref.ome.tiff,yes,DAPI|PANCK|SMA\n"
    )  # 'yes' not 'true'
print("  Created invalid_checkpoint_bad_ref.csv (invalid is_reference)")

# 4g. File does not exist
with open(OUT_DIR / "invalid_file_not_found.csv", "w") as f:
    f.write("patient_id,path_to_file,is_reference,channels\n")
    f.write("P001,/nonexistent/path/file.ome.tiff,true,DAPI|PANCK|SMA\n")
print("  Created invalid_file_not_found.csv (file not found)")

# =============================================================================
# 4h. TILED_COARSE tile-plan CSV fixtures, for tiled_adapter_group_size.nf.test's
#     countTileRows()/requirePositiveTileCount() coverage (subworkflows/local/adapters/
#     tiled_adapter.nf). Same header TILED_COARSE actually writes
#     (modules/local/tiled_coarse.nf), covering: a normal 12-row plan, one with a
#     trailing blank line, one with a blank line interspersed among data rows, and a
#     header-only plan (must hit the n < 1 guard).
# =============================================================================
print("\n4h. Creating tile-plan CSV fixtures for the tiled fan-in gather...")
_TILE_HEADER = "ix,iy,cx,cy,x0,y0,x1,y1,rx0,ry0,rx1,ry1"


def _tile_row(ix, iy):
    x0, y0 = ix * 16, iy * 16
    return f"{ix},{iy},{x0 + 8},{y0 + 8},{x0},{y0},{x0 + 16},{y0 + 16},{x0},{y0},{x0 + 16},{y0 + 16}"


_tile_rows = [_tile_row(i % 4, i // 4) for i in range(12)]

with open(OUT_DIR / "tiles_12_rows.csv", "w") as f:
    f.write(_TILE_HEADER + "\n")
    for r in _tile_rows:
        f.write(r + "\n")
print("  Created tiles_12_rows.csv (12 data rows)")

with open(OUT_DIR / "tiles_12_rows_trailing_blank.csv", "w") as f:
    f.write(_TILE_HEADER + "\n")
    for r in _tile_rows:
        f.write(r + "\n")
    f.write("\n")
print("  Created tiles_12_rows_trailing_blank.csv (12 data rows + trailing blank line)")

with open(OUT_DIR / "tiles_12_rows_blank_interspersed.csv", "w") as f:
    f.write(_TILE_HEADER + "\n")
    for i, r in enumerate(_tile_rows):
        f.write(r + "\n")
        if i == 5:
            f.write("\n")
print("  Created tiles_12_rows_blank_interspersed.csv (blank line mid-file)")

with open(OUT_DIR / "tiles_header_only.csv", "w") as f:
    f.write(_TILE_HEADER + "\n")
print("  Created tiles_header_only.csv (no data rows)")

# =============================================================================
# 5. Update test.config input to use valid data
# =============================================================================
print("\n5. Creating test.config input CSV...")
with open(OUT_DIR / "test_input.csv", "w") as f:
    f.write("patient_id,path_to_file,is_reference,channels\n")
    f.write(f"P001,{TESTDATA_ABS}/P001_ref.ome.tiff,true,DAPI|PANCK|SMA\n")
    f.write(f"P001,{TESTDATA_ABS}/P001_mov1.ome.tiff,false,DAPI|CD3|CD8\n")
print("  Created test_input.csv for test profile")

print("\n" + "=" * 70)
print("✓ Test data generation complete!")
print("=" * 70)
print(f"\nGenerated files in {OUT_DIR}:")
print("\nValid data:")
print("  - P001_ref.ome.tiff, P001_mov1.ome.tiff, P001_mov2.ome.tiff")
print("  - P002_ref.ome.tiff")
print("  - P001_cell_mask.npy, P002_cell_mask.npy")
print("  - valid_preprocessing.csv")
print("  - valid_checkpoint_registration.csv")
print("  - valid_checkpoint_segmented.csv")
print("  - valid_checkpoint_segmented_no_compartments.csv")
print("  - segmented.csv")
print("  - valid_checkpoint_postprocessing.csv")
print("  - test_input.csv")
print("\nInvalid data (for validation testing):")
print("  - invalid_multi_ref.csv")
print("  - invalid_no_ref.csv")
print("  - invalid_dapi_position.csv")
print("  - invalid_no_dapi.csv")
print("  - invalid_checkpoint_missing_col.csv")
print("  - invalid_checkpoint_bad_ref.csv")
print("  - invalid_file_not_found.csv")
print("\nTile-plan CSV fixtures:")
print("  - tiles_12_rows.csv, tiles_12_rows_trailing_blank.csv")
print("  - tiles_12_rows_blank_interspersed.csv, tiles_header_only.csv")
# =============================================================================
# 6. Generate additional test fixtures for module tests
# =============================================================================
print("\n6. Creating additional test fixtures for module tests...")

# 6a. Merged quantification CSV for export/QC module tests
with open(OUT_DIR / "sample_merged_quant.csv", "w") as f:
    f.write(
        "label,centroid_x,centroid_y,area,perimeter,eccentricity,major_axis,minor_axis,solidity,DAPI,PANCK,SMA\n"
    )
    for i in range(1, 21):
        cx = np.random.uniform(10, 118)
        cy = np.random.uniform(10, 118)
        area = np.random.randint(150, 350)
        perimeter = np.random.uniform(45, 75)
        eccentricity = np.random.uniform(0.3, 0.6)
        major = np.random.uniform(12, 25)
        minor = np.random.uniform(8, 18)
        solidity = np.random.uniform(0.85, 0.98)
        dapi = np.random.randint(6000, 12000)
        panck = np.random.randint(1500, 8000)
        sma = np.random.randint(1000, 5000)
        f.write(
            f"{i},{cx:.1f},{cy:.1f},{area},{perimeter:.1f},{eccentricity:.2f},{major:.1f},{minor:.1f},{solidity:.2f},{dapi},{panck},{sma}\n"
        )
print("  Created sample_merged_quant.csv (20 cells)")

# 6d. Sample features JSON
# 6h. Single channel TIF images (already exist but ensure proper format)
for ch_name in ["DAPI", "PANCK", "SMA"]:
    img = np.random.randint(100, 10000, size=(128, 128), dtype=np.uint16)
    tifffile.imwrite(OUT_DIR / f"sample_{ch_name}.tif", img, photometric="minisblack")
print("  Created single-channel sample TIFs")

# 6i. Channels text file
with open(OUT_DIR / "sample_channels.txt", "w") as f:
    f.write("DAPI\n")
    f.write("PANCK\n")
    f.write("SMA\n")
print("  Created sample_channels.txt")

print("\n" + "=" * 70)
print("Test data generation complete!")
print("=" * 70)
print(f"\nGenerated files in {OUT_DIR}:")
print("\nValid data:")
print("  - P001_ref.ome.tiff, P001_mov1.ome.tiff, P001_mov2.ome.tiff")
print("  - P002_ref.ome.tiff")
print("  - P001_cell_mask.npy, P002_cell_mask.npy")
print("  - valid_preprocessing.csv")
print("  - valid_checkpoint_registration.csv")
print("  - valid_checkpoint_segmented.csv")
print("  - valid_checkpoint_segmented_no_compartments.csv")
print("  - segmented.csv")
print("  - valid_checkpoint_postprocessing.csv")
print("  - test_input.csv")
print("\nInvalid data (for validation testing):")
print("  - invalid_multi_ref.csv")
print("  - invalid_no_ref.csv")
print("  - invalid_dapi_position.csv")
print("  - invalid_no_dapi.csv")
print("  - invalid_checkpoint_missing_col.csv")
print("  - invalid_checkpoint_bad_ref.csv")
print("  - invalid_file_not_found.csv")
print("\nModule test fixtures:")
print("  - sample_merged_quant.csv")
print("  - sample_DAPI.tif, sample_PANCK.tif, sample_SMA.tif")
print("  - sample_channels.txt")
# =============================================================================
# 7. Fixtures for updated postprocessing: EXTRACT_CELL_PROPERTIES, intensity-only
#    QUANTIFY, MERGE_QUANT_CSVS with morphology
# =============================================================================
print("\n7. Creating postprocessing fixtures (updated architecture)...")

# 7a. Morphology CSV from EXTRACT_CELL_PROPERTIES (matches 20-cell mask)
morphology_columns = [
    "label",
    "y",
    "x",
    "area",
    "eccentricity",
    "perimeter",
    "convex_area",
    "axis_major_length",
    "axis_minor_length",
]
morphology_rows = []
for i in range(1, 21):
    row = {
        "label": i,
        "y": round(np.random.uniform(10, 118), 1),
        "x": round(np.random.uniform(10, 118), 1),
        "area": int(np.random.randint(150, 350)),
        "eccentricity": round(np.random.uniform(0.3, 0.7), 3),
        "perimeter": round(np.random.uniform(45, 75), 1),
        "convex_area": int(np.random.randint(160, 370)),
        "axis_major_length": round(np.random.uniform(12, 25), 1),
        "axis_minor_length": round(np.random.uniform(8, 18), 1),
    }
    morphology_rows.append(row)

with open(OUT_DIR / "sample_morphology.csv", "w") as f:
    f.write(",".join(morphology_columns) + "\n")
    for row in morphology_rows:
        f.write(",".join(str(row[c]) for c in morphology_columns) + "\n")
print("  Created sample_morphology.csv (20 cells, morphology only)")

# 7b. Contours JSON from EXTRACT_CELL_PROPERTIES
contours = {}
for i in range(1, 21):
    cx = morphology_rows[i - 1]["x"]
    cy = morphology_rows[i - 1]["y"]
    r = np.random.uniform(5, 10)
    n_pts = 8
    angles = np.linspace(0, 2 * np.pi, n_pts, endpoint=False)
    coords = [
        [round(cx + r * np.cos(a), 1), round(cy + r * np.sin(a), 1)] for a in angles
    ]
    coords.append(coords[0])  # close polygon
    contours[str(i)] = {"coordinates": coords}

with open(OUT_DIR / "sample_contours.json", "w") as f:
    json.dump(contours, f, indent=2)
print("  Created sample_contours.json (20 cell polygons)")

# 7c. Intensity-only CSVs (new QUANTIFY format: just label + marker)
for ch_name, base_intensity in [("DAPI", 8000), ("PANCK", 4000), ("SMA", 2000)]:
    with open(OUT_DIR / f"sample_{ch_name}_intensity.csv", "w") as f:
        f.write(f"label,{ch_name}\n")
        for i in range(1, 21):
            val = round(base_intensity + np.random.uniform(-1000, 3000), 1)
            f.write(f"{i},{val}\n")
    print(f"  Created sample_{ch_name}_intensity.csv (20 cells, intensity only)")

# 7d. Empty samplesheet (header only, for validation tests)
with open(OUT_DIR / "empty_samplesheet.csv", "w") as f:
    f.write("patient_id,path_to_file,is_reference,channels\n")
print("  Created empty_samplesheet.csv")

# 7e. Single sample samplesheet (for single-sample tests)
with open(OUT_DIR / "single_sample.csv", "w") as f:
    f.write("patient_id,path_to_file,is_reference,channels\n")
    f.write(f"P001,{TESTDATA_ABS}/P001_ref.ome.tiff,true,DAPI|PANCK|SMA\n")
print("  Created single_sample.csv")

# 7f. Two-image, one-patient samplesheet where every row's patient_id cell
# carries a trailing space. Regression fixture for the silent-row-drop bug:
# Nextflow's splitCsv() does not trim fields, so an untrimmed
# CsvUtils.parseMetadata() would produce meta.patient_id == "P001 " while the
# pre-computed per-patient counts map (built via CsvUtils.parseCsvLine, which
# does trim) keys on "P001" — the inner-join combine(by: 0) in
# workflows/mirage.nf's loadInputChannel() then silently drops the row.
with open(OUT_DIR / "whitespace_patient_id.csv", "w") as f:
    f.write("patient_id,path_to_file,is_reference,channels\n")
    f.write(f"P001 ,{TESTDATA_ABS}/P001_ref.ome.tiff,true,DAPI|PANCK|SMA\n")
    f.write(f"P001 ,{TESTDATA_ABS}/P001_mov1.ome.tiff,false,DAPI|CD3|CD8\n")
print("  Created whitespace_patient_id.csv")

# 7g-bis. Nuclear-marker fixtures. `params.nuclear_markers` is the declared source of
# truth for "which channel is nuclear", and its shipped default is ['DAPI', 'CELLTOX'] --
# but every consumer used to hardcode the literal 'DAPI', so the CELLTOX half of the
# default was unreachable. These two sheets exercise it.
#
# celltox_nonreference.csv is the dangerous shape: the ONLY CELLTOX-bearing slide is a
# NON-reference one, on the SHIPPED DEFAULT marker list. Under the keep-set rule
# (CsvUtils.resolveKeptChannelsPerSlide) nuclear-ness plays no part in the drop decision,
# so the reference claims {DAPI,PANCK,SMA} and the moving slide keeps {CELLTOX,CD3,CD8}
# -- SIX markers, and bin/split_multichannel.py must emit exactly those six. (This
# comment used to say five, describing the rule that PRECEDED the keep-set: it dropped
# every nuclear-matching channel from every non-reference slide, silently discarding a
# cohort's second nuclear stain. tests/main.nf.test asserts six.)
with open(OUT_DIR / "celltox_nonreference.csv", "w") as f:
    f.write("patient_id,path_to_file,is_reference,channels\n")
    f.write(f"P001,{TESTDATA_ABS}/P001_ref.ome.tiff,true,DAPI|PANCK|SMA\n")
    f.write(f"P001,{TESTDATA_ABS}/P001_mov1.ome.tiff,false,CELLTOX|CD3|CD8\n")
print("  Created celltox_nonreference.csv")

# celltox_only.csv has no DAPI anywhere: run it with --nuclear_markers CELLTOX. Before
# the nuclear-marker rule was shared, CsvUtils rejected this sheet at launch ("DAPI
# channel not found") and SEGMENT rejected it again at runtime.
with open(OUT_DIR / "celltox_only.csv", "w") as f:
    f.write("patient_id,path_to_file,is_reference,channels\n")
    f.write(f"P001,{TESTDATA_ABS}/P001_ref.ome.tiff,true,CELLTOX|PANCK|SMA\n")
    f.write(f"P001,{TESTDATA_ABS}/P001_mov1.ome.tiff,false,CELLTOX|CD3|CD8\n")
print("  Created celltox_only.csv")

# 7g-ter. Keep-set regression sheets. Both target the ONE invariant the keep-set design
# rests on: EVERY MARKER NAME IS EMITTED EXACTLY ONCE PER PATIENT, which is what makes
# meta.channels_count (CsvUtils.countChannelsPerPatient) exact against the TIFFs that
# actually arrive.
#
# keepset_exhausted.csv: the moving slide carries ONLY DAPI, which the reference has
# already claimed, so its keep-set resolves to the EMPTY list -- the slide contributes no
# new markers and countChannelsPerPatient counts it as zero (patient total 3). The empty
# list used to be indistinguishable from "no entry" at the lookup site, whose `?:` fell
# back to the slide's FULL declared list (Groovy treats [] as falsy): DAPI was emitted a
# SECOND time, giving 4 QUANTIFY tasks against a channels_count of 3, and which slide's
# DAPI reached merged_quant.csv and the pyramid came down to arrival order.
with open(OUT_DIR / "keepset_exhausted.csv", "w") as f:
    f.write("patient_id,path_to_file,is_reference,channels\n")
    f.write(f"P001,{TESTDATA_ABS}/P001_ref.ome.tiff,true,DAPI|PANCK|SMA\n")
    f.write(f"P001,{TESTDATA_ABS}/P001_mov_dapi_only.ome.tiff,false,DAPI\n")
print("  Created keepset_exhausted.csv")

# duplicate_basename.csv: two slides of one patient sharing the basename slide.ome.tiff
# under cycle1/ and cycle2/. Patient total is 3 (DAPI+CD3 from the reference, CD8 from
# cycle2). While the per-slide map was keyed on the BASENAME the two rows collapsed onto
# one entry, so the reference was handed cycle2's keep-set and emitted zero channels.
with open(OUT_DIR / "duplicate_basename.csv", "w") as f:
    f.write("patient_id,path_to_file,is_reference,channels\n")
    f.write(f"P001,{TESTDATA_ABS}/cycle1/slide.ome.tiff,true,DAPI|CD3\n")
    f.write(f"P001,{TESTDATA_ABS}/cycle2/slide.ome.tiff,false,DAPI|CD8\n")
print("  Created duplicate_basename.csv")

# duplicate_raw_path.csv: two rows of one patient sharing the EXACT SAME raw
# path_to_file cell (not just the same basename -- the literal identical string),
# an ordinary copy-paste data-entry error that validateInputSemantics does not
# reject. This is the fix-round-1 regression for Task 4.4: CsvUtils.rowIndexPerPatient
# used to key a SCALAR by "patientId::rawImageCell", so the second row's index
# silently overwrote the first's (last write wins) and BOTH rows read back the SAME
# index -- Meta.fromSamplesheetRow then assigned them the SAME meta.id, and the
# reference row's real keep-set ([DAPI, CD3]) was silently displaced by the second
# row's ([], since its only channel DAPI is already claimed) under that shared id.
# Patient total is 2 (DAPI+CD3 from the reference; the duplicate row contributes
# nothing new).
with open(OUT_DIR / "duplicate_raw_path.csv", "w") as f:
    f.write("patient_id,path_to_file,is_reference,channels\n")
    f.write(f"P001,{TESTDATA_ABS}/P001_ref.ome.tiff,true,DAPI|CD3\n")
    f.write(f"P001,{TESTDATA_ABS}/P001_ref.ome.tiff,false,DAPI\n")
print("  Created duplicate_raw_path.csv")

# 7g. Registration QC fixtures matching WARP_SEG_QC.out.metrics / .out.per_cell,
# so tests/subworkflows/local/postprocessing.nf.test can pass non-empty
# ch_reg_qc / ch_reg_residuals into POSTPROCESSING and exercise the
# EXPORT_SPATIALDATA fold-in path (postprocess.nf ~379-384), not just
# Channel.empty(). Shape mirrors what bin/warp_seg_qc.py's build_record()
# actually emits (run()'s return at bin/warp_seg_qc.py:349-367, splatted at
# :407) — per-stage metrics nested under "stages", keyed by stage name, with
# sibling "stage_order"/"delta_vs_anchor"/"matching"/"counts"/"params" — not
# top-level stage keys. Field names inside each stage record come from
# summarize_stage()/_dist_stats() in bin/utils/cell_pairs.py:340-384
# (n_pairs, n_pairs_scored, iou_n/iou_mean/iou_p10/iou_p50/iou_p90/iou_max,
# displacement_px_n/_mean/_p10/_p50/_p90/_max, frac_iou_ge_<thresh>,
# dice_matched) — NOT the mean_iou/mean_residual_px names used in an earlier,
# invented version of this fixture. Cross-checked against the module's own
# -stub block (modules/local/warp_seg_qc.nf:85-97), which builds the same
# top-level shape (stage_order/stages/delta_vs_anchor/matching/counts).
ANCHOR_STAGE = "rigid"
STAGE_ORDER = ["native", "rigid", "non_rigid", "micro"]
_DELTA_KEYS = (
    "iou_mean",
    "iou_p50",
    "displacement_px_p50",
    "displacement_px_p90",
    "displacement_um_p50",
    "displacement_um_p90",
    "dice_matched",
)


def _stage_record(n_pairs, n_scored, iou_mean, iou_p10, iou_p50, iou_p90, iou_max,
                   disp_mean, disp_p10, disp_p50, disp_p90, disp_max, dice):
    return {
        "n_pairs": n_pairs,
        "n_pairs_scored": n_scored,
        "iou_n": n_scored,
        "iou_mean": iou_mean,
        "iou_p10": iou_p10,
        "iou_p50": iou_p50,
        "iou_p90": iou_p90,
        "iou_max": iou_max,
        "displacement_px_n": n_scored,
        "displacement_px_mean": disp_mean,
        "displacement_px_p10": disp_p10,
        "displacement_px_p50": disp_p50,
        "displacement_px_p90": disp_p90,
        "displacement_px_max": disp_max,
        "frac_iou_ge_0.5": round(min(1.0, iou_mean + 0.1), 3),
        "dice_matched": dice,
    }


stage_records = {
    "native": _stage_record(20, 18, 0.41, 0.20, 0.40, 0.62, 0.70, 18.2, 9.5, 17.8, 27.6, 34.0, 0.39),
    "rigid": _stage_record(20, 19, 0.68, 0.50, 0.69, 0.85, 0.92, 6.4, 2.1, 6.0, 11.2, 14.5, 0.66),
    "non_rigid": _stage_record(20, 19, 0.83, 0.68, 0.84, 0.94, 0.97, 2.1, 0.6, 1.9, 3.8, 5.0, 0.82),
    "micro": _stage_record(20, 20, 0.91, 0.80, 0.92, 0.98, 0.99, 0.9, 0.2, 0.8, 1.7, 2.3, 0.90),
}
anchor_rec = stage_records[ANCHOR_STAGE]
delta_vs_anchor = {
    stage: {
        k: round(stage_records[stage][k] - anchor_rec[k], 4)
        for k in _DELTA_KEYS
        if k in stage_records[stage] and k in anchor_rec
    }
    for stage in STAGE_ORDER
    if stage != ANCHOR_STAGE
}
seg_qc_record = {
    "patient_id": "P001",
    "moving": "P001_mov1.ome.tiff",
    "reference": "P001_ref.ome.tiff",
    "stages_separable": True,
    "stage_order": STAGE_ORDER,
    "stages": stage_records,
    "delta_vs_anchor": delta_vs_anchor,
    "matching": {
        "method": "lsa_centroid",
        "anchor_stage": ANCHOR_STAGE,
        "n_pairs": 20,
    },
    "counts": {"features_ref": 22, "features_moving": 21},
    "params": {
        "iou_thresh": 0.5,
        "supersample": 2,
        "max_pair_window_px": 4_000_000,
        "pixel_size_um": 0.325,
    },
    "micro_reg": 1,
    "rigid_includes_micro_rigid": True,
}
with open(OUT_DIR / "sample_reg_qc.json", "w") as f:
    json.dump(seg_qc_record, f, indent=2)
print("  Created sample_reg_qc.json")

# Per-patient CSE seg-eval JSONs, the input MERGE_SEG_EVAL merges.
#
# Referenced by tests/modules/merge_seg_eval.nf.test with checkIfExists: true and
# produced by nothing -- stranded when the CSE removal in cea0b47 took the writer
# away and left the reader. That was 2 of the 3 nf-test failures on this branch.
#
# Generated, never committed: tests/testdata/ is gitignored (.gitignore:140,142),
# so a hand-written fixture exists on one machine and is absent in CI -- which is
# a guard that cannot run, the same failure this file exists to prevent.
#
# The shape is bin/seg_quality_eval.py's `doc`, and it must survive
# bin/merge_seg_eval.py's flatten(): `id` is read directly and would KeyError if
# renamed, `metrics` is flattened one level into `metrics::<key>` columns, and
# QualityScore/downsample_factor/effective_pixel_size_um each become a column.
# The two patients deliberately carry DIFFERENT downsample factors, because
# carrying the factor per patient is the whole reason those columns exist -- a
# fixture where both agree would not notice the column being dropped.
for _pid, _qs, _factor in (("P001", 0.7421, 1), ("P002", 0.6183, 4)):
    _seg_eval = {
        "id": _pid,
        "metrics": {
            "QualityScore": _qs,
            "NumberOfCellsPer100SquareMicrons": 0.9312,
            "FractionOfForegroundOccupiedByCells": 0.6428,
            "1minusFractionOfBackgroundOccupiedByCells": 0.8871,
            "FractionOfCellMaskInForeground": 0.9604,
            "1minusFractionOfCellsWithoutNucleus": 0.9750,
        },
        "QualityScore": _qs,
        "downsample_factor": _factor,
        "effective_pixel_size_um": round(0.325 * _factor, 6),
    }
    with open(OUT_DIR / f"seg_eval_{_pid}.json", "w") as f:
        json.dump(_seg_eval, f, indent=2)
    print(f"  Created seg_eval_{_pid}.json")

with open(OUT_DIR / "sample_reg_residuals.csv", "w") as f:
    f.write("moving,ref_x,ref_y,residual_px,stage\n")
    rng_res = np.random.default_rng(7)
    for i in range(10):
        x = round(float(rng_res.uniform(10, 118)), 4)
        y = round(float(rng_res.uniform(10, 118)), 4)
        d = round(float(rng_res.uniform(0.2, 2.5)), 6)
        f.write(f"P001_mov1.ome.tiff,{x},{y},{d},micro\n")
print("  Created sample_reg_residuals.csv")

# =============================================================================
# 8. Golden reference files in tests/testdata/expected/
# =============================================================================
print("\n8. Creating golden reference files in expected/...")

# 8a. Expected channel list
with open(EXPECTED_DIR / "channels_3ch.txt", "w") as f:
    f.write("DAPI\nPANCK\nSMA\n")
print("  Created expected/channels_3ch.txt")

# 8d. Expected merged quant columns (fov + cell_size + morphology + markers)
with open(EXPECTED_DIR / "merged_quant_columns.txt", "w") as f:
    f.write(
        "fov,cell_size,label,y,x,area,eccentricity,perimeter,convex_area,axis_major_length,axis_minor_length,DAPI,PANCK,SMA\n"
    )
print("  Created expected/merged_quant_columns.txt")

# 8e. Expected morphology columns
with open(EXPECTED_DIR / "morphology_columns.txt", "w") as f:
    f.write(",".join(morphology_columns) + "\n")
print("  Created expected/morphology_columns.txt")

# 8f. Expected intensity CSV columns (template — substitute channel name)
with open(EXPECTED_DIR / "intensity_csv_columns.txt", "w") as f:
    f.write("label,{CHANNEL}\n")
print("  Created expected/intensity_csv_columns.txt")

# 8g. Expected preprocessing checkpoint columns
with open(EXPECTED_DIR / "preproc_checkpoint_columns.txt", "w") as f:
    f.write("patient_id,id,preprocessed_image,is_reference,channels\n")
print("  Created expected/preproc_checkpoint_columns.txt")

# 8h. Expected registration checkpoint columns
with open(EXPECTED_DIR / "reg_checkpoint_columns.txt", "w") as f:
    f.write("patient_id,id,registered_image,is_reference,channels\n")
print("  Created expected/reg_checkpoint_columns.txt")

# =============================================================================
# 9. Shipped-defaults smoke fixture -- the ONE pair of images in this whole
#    generator that carries a real OME PhysicalSizeX/Y.
# =============================================================================
# Every other fixture above is deliberately scale-less (`auto` would correctly
# hard-fail at PREFLIGHT_SCALE on any of them -- see conf/test.config). CI's own
# `-profile test` therefore never exercises the shipped defaults (`pixel_size =
# 'auto'`, `seg_method = 'instantseg'`) together, because it pins both away from
# them. This pair -- and the samplesheet naming it -- exists so a dedicated CI
# job can run the stub pipeline with NEITHER pin, giving `auto`'s happy path its
# first real (if stub-mode) end-to-end coverage.
print("\n9. Creating shipped-defaults smoke fixture (real OME PhysicalSizeX/Y)...")
p900_anatomy = make_anatomy((128, 128), n_cells=40, rng=_img_rng)
create_multichannel_image(
    OUT_DIR / "P900_ref_scaled.ome.tiff",
    p900_anatomy,
    channel_names=["DAPI", "PANCK", "SMA"],
    shift=(0, 0),
    rng=_img_rng,
    pixel_size_um=0.325,
)
create_multichannel_image(
    OUT_DIR / "P900_mov_scaled.ome.tiff",
    p900_anatomy,
    channel_names=["DAPI", "CD3", "CD8"],
    shift=(5, 5),
    rng=_img_rng,
    pixel_size_um=0.325,
)
with open(OUT_DIR / "shipped_defaults_input.csv", "w") as f:
    f.write("patient_id,path_to_file,is_reference,channels\n")
    f.write(f"P900,{TESTDATA_ABS}/P900_ref_scaled.ome.tiff,true,DAPI|PANCK|SMA\n")
    f.write(f"P900,{TESTDATA_ABS}/P900_mov_scaled.ome.tiff,false,DAPI|CD3|CD8\n")
print("  Created shipped_defaults_input.csv (pixel_size='auto' happy path)")

print("\n" + "=" * 70)
print("All test data generation complete!")
print("=" * 70)

print("\nThese files can be used to test:")
print("  1. Full pipeline execution with -profile test")
print("  2. Individual process testing with nf-test")
print("  3. Input validation and error handling")
print("  4. Module-level unit tests")
print("  5. Golden file comparison for output correctness")
