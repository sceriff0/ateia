#!/usr/bin/env python3
"""Generate complete test fixtures for full pipeline testing.

Creates realistic test data including:
- Multi-channel OME-TIFF images (reference and moving)
- Segmentation masks
- CSV files for all pipeline entry points
- Both valid and invalid test cases for validation testing
"""
import os
import json
import numpy as np
import tifffile
from pathlib import Path

# Deterministic seed — ensures identical test data across runs and CI environments
np.random.seed(42)

OUT_DIR = Path(__file__).parent
EXPECTED_DIR = OUT_DIR / 'expected'
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
        radius = int(rng.integers(3, 8))
        intensity = int(rng.integers(8000, 15000))  # shared base, scaled per channel
        cells.append((cy, cx, radius, intensity))
    return cells


def _render_channel(anatomy, size, shift, scale, rng, add_noise=True):
    """Render one channel: draw the shared anatomy translated by ``shift`` with
    per-channel intensity ``scale`` (DAPI=1.0; markers dimmer/variable)."""
    img = np.zeros(size, dtype=np.uint16)
    yy, xx = np.ogrid[:size[0], :size[1]]
    for (cy, cx, radius, intensity) in anatomy:
        cy_s, cx_s = cy + shift[0], cx + shift[1]
        dist2 = (yy - cy_s) ** 2 + (xx - cx_s) ** 2
        circle_mask = dist2 <= radius ** 2
        gaussian = np.exp(-dist2 / (2 * (radius / 2) ** 2))
        img = np.maximum(img, (circle_mask * gaussian * intensity * scale).astype(np.uint16))
    if add_noise:
        noise = rng.normal(100, 20, size).astype(np.int32)
        img = np.clip(img.astype(np.int32) + noise, 0, 65535).astype(np.uint16)
    return img


def create_multichannel_image(filename, anatomy, size=(128, 128), channel_names=None,
                              add_noise=True, shift=(0, 0), rng=None):
    """Create a multi-channel OME-TIFF from a SHARED anatomy translated by ``shift``.

    Channel 0 (DAPI) is the registration reference channel: rendered at full
    intensity (scale 1.0) from the shared anatomy, it is near-identical across a
    patient's images up to the known ``shift`` (+ light noise), so VALIS can
    recover the transform. Marker channels reuse the same geometry at lower,
    channel-specific intensities (co-registered marker panels).
    """
    if channel_names is None:
        channel_names = ['DAPI', 'PANCK', 'SMA']
    if rng is None:
        rng = _img_rng
    channels = []
    for ch in range(len(channel_names)):
        scale = 1.0 if ch == 0 else float(rng.uniform(0.25, 0.7))
        channels.append(_render_channel(anatomy, size, shift, scale, rng, add_noise))

    # Stack channels (C, Y, X)
    multichannel = np.stack(channels, axis=0)

    # Save as OME-TIFF with proper metadata
    tifffile.imwrite(
        filename,
        multichannel,
        photometric='minisblack',
        metadata={'axes': 'CYX', 'Channel': {'Name': channel_names}}
    )
    print(f"  Created {filename} - shape: {multichannel.shape}, channels: {channel_names}")
    return multichannel

# Patient P001 - Reference + 2 moving images. All share ONE anatomy so the DAPI
# channel of each moving image is the reference DAPI translated by `shift`
# (true correspondence for VALIS registration). Each slide keeps its own marker
# panel. n_cells is generous so SuperPoint/SuperGlue has plenty of keypoints.
print("\n1. Creating multi-channel OME-TIFF images...")
p001_anatomy = make_anatomy((128, 128), n_cells=40, rng=_img_rng)
create_multichannel_image(OUT_DIR / 'P001_ref.ome.tiff',  p001_anatomy, channel_names=['DAPI', 'PANCK', 'SMA'],      shift=(0, 0),  rng=_img_rng)
create_multichannel_image(OUT_DIR / 'P001_mov1.ome.tiff', p001_anatomy, channel_names=['DAPI', 'CD3', 'CD8'],        shift=(5, 5),  rng=_img_rng)
create_multichannel_image(OUT_DIR / 'P001_mov2.ome.tiff', p001_anatomy, channel_names=['DAPI', 'VIMENTIN', 'CD45'],  shift=(-3, 4), rng=_img_rng)

# Patient P002 - Single slide (reference only); its own shared anatomy.
p002_anatomy = make_anatomy((128, 128), n_cells=40, rng=_img_rng)
create_multichannel_image(OUT_DIR / 'P002_ref.ome.tiff',  p002_anatomy, channel_names=['DAPI', 'PANCK', 'SMA'],      shift=(0, 0),  rng=_img_rng)

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
        cy = np.random.randint(10, size[0]-10)
        cx = np.random.randint(10, size[1]-10)
        radius = np.random.randint(4, 9)

        # Check for overlap
        yy, xx = np.ogrid[:size[0], :size[1]]
        circle_mask = (yy - cy)**2 + (xx - cx)**2 <= radius**2

        if mask[circle_mask].sum() == 0:  # No overlap
            mask[circle_mask] = label
            label += 1

    np.save(filename, mask)
    # Production segmentation masks come from SEGMENT as uint32 TIFFs (segment.py
    # writes *_cell_mask.tif with compression='zlib'). Modules that read the mask
    # as an image (e.g. MERGE_AND_PYRAMID) need a TIFF, not the .npy fixture, so
    # write a matching .tif alongside it.
    tif_path = Path(filename).with_suffix('.tif')
    tifffile.imwrite(tif_path, mask.astype(np.uint32), compression='zlib')
    print(f"  Created {filename} and {tif_path.name} - {label-1} cells")
    return mask

create_segmentation_mask(OUT_DIR / 'P001_cell_mask.npy', n_cells=20)
create_segmentation_mask(OUT_DIR / 'P002_cell_mask.npy', n_cells=15)

# =============================================================================
# 3. Generate valid input CSVs for each pipeline entry point
# =============================================================================
print("\n3. Creating valid input CSVs...")

# Use absolute paths resolved from the output directory
# This ensures CSVs work regardless of where the pipeline is launched from
TESTDATA_ABS = str(OUT_DIR.resolve())

# 3a. Valid input for preprocessing step (ND2 conversion disabled)
with open(OUT_DIR / 'valid_preprocessing.csv', 'w') as f:
    f.write('patient_id,path_to_file,is_reference,channels\n')
    f.write(f'P001,{TESTDATA_ABS}/P001_ref.ome.tiff,true,DAPI|PANCK|SMA\n')
    f.write(f'P001,{TESTDATA_ABS}/P001_mov1.ome.tiff,false,DAPI|CD3|CD8\n')
    f.write(f'P001,{TESTDATA_ABS}/P001_mov2.ome.tiff,false,DAPI|VIMENTIN|CD45\n')
    f.write(f'P002,{TESTDATA_ABS}/P002_ref.ome.tiff,true,DAPI|PANCK|SMA\n')
print(f"  Created valid_preprocessing.csv")

# 3b. Valid checkpoint CSV for registration step
with open(OUT_DIR / 'valid_checkpoint_registration.csv', 'w') as f:
    f.write('patient_id,preprocessed_image,is_reference,channels\n')
    f.write(f'P001,{TESTDATA_ABS}/P001_ref.ome.tiff,true,DAPI|PANCK|SMA\n')
    f.write(f'P001,{TESTDATA_ABS}/P001_mov1.ome.tiff,false,DAPI|CD3|CD8\n')
print(f"  Created valid_checkpoint_registration.csv")

# 3c. Valid checkpoint CSV for postprocessing step
with open(OUT_DIR / 'valid_checkpoint_postprocessing.csv', 'w') as f:
    f.write('patient_id,registered_image,is_reference,channels\n')
    f.write(f'P001,{TESTDATA_ABS}/P001_ref.ome.tiff,true,DAPI|PANCK|SMA\n')
print(f"  Created valid_checkpoint_postprocessing.csv")

# =============================================================================
# 4. Generate INVALID input CSVs for validation testing
# =============================================================================
print("\n4. Creating invalid input CSVs for validation tests...")

# 4a. Multiple references per patient
with open(OUT_DIR / 'invalid_multi_ref.csv', 'w') as f:
    f.write('patient_id,path_to_file,is_reference,channels\n')
    f.write(f'P001,{TESTDATA_ABS}/P001_ref.ome.tiff,true,DAPI|PANCK|SMA\n')
    f.write(f'P001,{TESTDATA_ABS}/P001_mov1.ome.tiff,true,DAPI|CD3|CD8\n')  # SECOND REF!
    f.write(f'P001,{TESTDATA_ABS}/P001_mov2.ome.tiff,false,DAPI|VIMENTIN|CD45\n')
print(f"  Created invalid_multi_ref.csv (multiple references)")

# 4b. No reference per patient
with open(OUT_DIR / 'invalid_no_ref.csv', 'w') as f:
    f.write('patient_id,path_to_file,is_reference,channels\n')
    f.write(f'P001,{TESTDATA_ABS}/P001_mov1.ome.tiff,false,DAPI|CD3|CD8\n')
    f.write(f'P001,{TESTDATA_ABS}/P001_mov2.ome.tiff,false,DAPI|VIMENTIN|CD45\n')
print(f"  Created invalid_no_ref.csv (no reference)")

# 4c. DAPI not in channel 0 (pre-converted OME-TIFF input)
with open(OUT_DIR / 'invalid_dapi_position.csv', 'w') as f:
    f.write('patient_id,path_to_file,is_reference,channels\n')
    f.write(f'P001,{TESTDATA_ABS}/P001_ref.ome.tiff,true,PANCK|DAPI|SMA\n')  # DAPI NOT FIRST
print(f"  Created invalid_dapi_position.csv (DAPI not in position 0)")

# 4d. Missing DAPI channel
with open(OUT_DIR / 'invalid_no_dapi.csv', 'w') as f:
    f.write('patient_id,path_to_file,is_reference,channels\n')
    f.write(f'P001,{TESTDATA_ABS}/P001_ref.ome.tiff,true,PANCK|SMA\n')  # NO DAPI
print(f"  Created invalid_no_dapi.csv (missing DAPI)")

# 4e. Invalid checkpoint - missing required column
with open(OUT_DIR / 'invalid_checkpoint_missing_col.csv', 'w') as f:
    f.write('patient_id,preprocessed_image,is_reference\n')  # Missing 'channels'
    f.write(f'P001,{TESTDATA_ABS}/P001_ref.ome.tiff,true\n')
print(f"  Created invalid_checkpoint_missing_col.csv (missing column)")

# 4f. Invalid checkpoint - malformed is_reference
with open(OUT_DIR / 'invalid_checkpoint_bad_ref.csv', 'w') as f:
    f.write('patient_id,preprocessed_image,is_reference,channels\n')
    f.write(f'P001,{TESTDATA_ABS}/P001_ref.ome.tiff,yes,DAPI|PANCK|SMA\n')  # 'yes' not 'true'
print(f"  Created invalid_checkpoint_bad_ref.csv (invalid is_reference)")

# 4g. File does not exist
with open(OUT_DIR / 'invalid_file_not_found.csv', 'w') as f:
    f.write('patient_id,path_to_file,is_reference,channels\n')
    f.write(f'P001,/nonexistent/path/file.ome.tiff,true,DAPI|PANCK|SMA\n')
print(f"  Created invalid_file_not_found.csv (file not found)")

# =============================================================================
# 5. Update test.config input to use valid data
# =============================================================================
print("\n5. Creating test.config input CSV...")
with open(OUT_DIR / 'test_input.csv', 'w') as f:
    f.write('patient_id,path_to_file,is_reference,channels\n')
    f.write(f'P001,{TESTDATA_ABS}/P001_ref.ome.tiff,true,DAPI|PANCK|SMA\n')
    f.write(f'P001,{TESTDATA_ABS}/P001_mov1.ome.tiff,false,DAPI|CD3|CD8\n')
print(f"  Created test_input.csv for test profile")

print("\n" + "="*70)
print("✓ Test data generation complete!")
print("="*70)
print(f"\nGenerated files in {OUT_DIR}:")
print("\nValid data:")
print("  - P001_ref.ome.tiff, P001_mov1.ome.tiff, P001_mov2.ome.tiff")
print("  - P002_ref.ome.tiff")
print("  - P001_cell_mask.npy, P002_cell_mask.npy")
print("  - valid_preprocessing.csv")
print("  - valid_checkpoint_registration.csv")
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
# =============================================================================
# 6. Generate additional test fixtures for module tests
# =============================================================================
print("\n6. Creating additional test fixtures for module tests...")

# 6a. Merged quantification CSV for phenotype tests
with open(OUT_DIR / 'sample_merged_quant.csv', 'w') as f:
    f.write('label,centroid_x,centroid_y,area,perimeter,eccentricity,major_axis,minor_axis,solidity,DAPI,PANCK,SMA\n')
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
        f.write(f'{i},{cx:.1f},{cy:.1f},{area},{perimeter:.1f},{eccentricity:.2f},{major:.1f},{minor:.1f},{solidity:.2f},{dapi},{panck},{sma}\n')
print(f"  Created sample_merged_quant.csv (20 cells)")

# 6b. Max dimensions file for padding tests
with open(OUT_DIR / 'sample_max_dims.txt', 'w') as f:
    f.write('MAX_HEIGHT 256\n')
    f.write('MAX_WIDTH 256\n')
print(f"  Created sample_max_dims.txt")

# 6c. Individual dimensions file
with open(OUT_DIR / 'sample_dims.txt', 'w') as f:
    f.write('P001_ref.ome.tiff 128 128\n')
    f.write('P001_mov1.ome.tiff 128 128\n')
print(f"  Created sample_dims.txt")

# 6d. Sample features JSON
features = {
    "keypoints": [
        {"x": 25.5, "y": 30.2, "descriptor": [0.1] * 256},
        {"x": 50.1, "y": 45.8, "descriptor": [0.2] * 256},
        {"x": 80.3, "y": 70.5, "descriptor": [0.3] * 256},
        {"x": 100.0, "y": 95.2, "descriptor": [0.15] * 256},
        {"x": 60.5, "y": 110.8, "descriptor": [0.25] * 256}
    ],
    "detector": "superpoint",
    "n_features": 5
}
with open(OUT_DIR / 'sample_features.json', 'w') as f:
    json.dump(features, f, indent=2)
print(f"  Created sample_features.json (5 keypoints)")

# 6e. Phenotype mapping JSON
phenotype_mapping = {
    "phenotypes": {
        "0": "Unassigned",
        "1": "PANCK+",
        "2": "SMA+",
        "3": "PANCK+SMA+"
    },
    "colors": {
        "0": "#808080",
        "1": "#00FF00",
        "2": "#FF0000",
        "3": "#FFFF00"
    }
}
with open(OUT_DIR / 'sample_phenotype_mapping.json', 'w') as f:
    json.dump(phenotype_mapping, f, indent=2)
print(f"  Created sample_phenotype_mapping.json")

# 6f. Sample GeoJSON for phenotype output
geojson = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [[[50, 30], [55, 30], [55, 35], [50, 35], [50, 30]]]
            },
            "properties": {
                "objectType": "cell",
                "classification": {"name": "PANCK+", "colorRGB": 65280},
                "measurements": [{"name": "DAPI", "value": 8500}]
            }
        }
    ]
}
with open(OUT_DIR / 'sample_phenotypes.geojson', 'w') as f:
    json.dump(geojson, f, indent=2)
print(f"  Created sample_phenotypes.geojson")

# 6g. Feature distance metrics JSON
metrics = {
    "pre_registration": {
        "mean_distance": 15.3,
        "median_distance": 12.8,
        "std_distance": 5.2,
        "n_matches": 45
    },
    "post_registration": {
        "mean_distance": 2.1,
        "median_distance": 1.8,
        "std_distance": 0.9,
        "n_matches": 45
    },
    "improvement": 86.3
}
with open(OUT_DIR / 'sample_feature_metrics.json', 'w') as f:
    json.dump(metrics, f, indent=2)
print(f"  Created sample_feature_metrics.json")

# 6h. Single channel TIF images (already exist but ensure proper format)
for ch_name in ['DAPI', 'PANCK', 'SMA']:
    img = np.random.randint(100, 10000, size=(128, 128), dtype=np.uint16)
    tifffile.imwrite(OUT_DIR / f'sample_{ch_name}.tif', img, photometric='minisblack')
print(f"  Created single-channel sample TIFs")

# 6i. Channels text file
with open(OUT_DIR / 'sample_channels.txt', 'w') as f:
    f.write('DAPI\n')
    f.write('PANCK\n')
    f.write('SMA\n')
print(f"  Created sample_channels.txt")

print("\n" + "="*70)
print("Test data generation complete!")
print("="*70)
print(f"\nGenerated files in {OUT_DIR}:")
print("\nValid data:")
print("  - P001_ref.ome.tiff, P001_mov1.ome.tiff, P001_mov2.ome.tiff")
print("  - P002_ref.ome.tiff")
print("  - P001_cell_mask.npy, P002_cell_mask.npy")
print("  - valid_preprocessing.csv")
print("  - valid_checkpoint_registration.csv")
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
print("  - sample_max_dims.txt")
print("  - sample_dims.txt")
print("  - sample_features.json")
print("  - sample_phenotype_mapping.json")
print("  - sample_phenotypes.geojson")
print("  - sample_feature_metrics.json")
print("  - sample_DAPI.tif, sample_PANCK.tif, sample_SMA.tif")
print("  - sample_channels.txt")
# =============================================================================
# 7. Fixtures for updated postprocessing: EXTRACT_CELL_PROPERTIES, intensity-only
#    QUANTIFY, MERGE_QUANT_CSVS with morphology, PHENOTYPE with contours
# =============================================================================
print("\n7. Creating postprocessing fixtures (updated architecture)...")

# 7a. Morphology CSV from EXTRACT_CELL_PROPERTIES (matches 20-cell mask)
morphology_columns = ['label', 'y', 'x', 'area', 'eccentricity', 'perimeter',
                      'convex_area', 'axis_major_length', 'axis_minor_length']
morphology_rows = []
for i in range(1, 21):
    row = {
        'label': i,
        'y': round(np.random.uniform(10, 118), 1),
        'x': round(np.random.uniform(10, 118), 1),
        'area': int(np.random.randint(150, 350)),
        'eccentricity': round(np.random.uniform(0.3, 0.7), 3),
        'perimeter': round(np.random.uniform(45, 75), 1),
        'convex_area': int(np.random.randint(160, 370)),
        'axis_major_length': round(np.random.uniform(12, 25), 1),
        'axis_minor_length': round(np.random.uniform(8, 18), 1),
    }
    morphology_rows.append(row)

with open(OUT_DIR / 'sample_morphology.csv', 'w') as f:
    f.write(','.join(morphology_columns) + '\n')
    for row in morphology_rows:
        f.write(','.join(str(row[c]) for c in morphology_columns) + '\n')
print("  Created sample_morphology.csv (20 cells, morphology only)")

# 7b. Contours JSON from EXTRACT_CELL_PROPERTIES
contours = {}
for i in range(1, 21):
    cx = morphology_rows[i-1]['x']
    cy = morphology_rows[i-1]['y']
    r = np.random.uniform(5, 10)
    n_pts = 8
    angles = np.linspace(0, 2*np.pi, n_pts, endpoint=False)
    coords = [[round(cx + r * np.cos(a), 1), round(cy + r * np.sin(a), 1)] for a in angles]
    coords.append(coords[0])  # close polygon
    contours[str(i)] = {"coordinates": coords}

with open(OUT_DIR / 'sample_contours.json', 'w') as f:
    json.dump(contours, f, indent=2)
print("  Created sample_contours.json (20 cell polygons)")

# 7c. Intensity-only CSVs (new QUANTIFY format: just label + marker)
for ch_name, base_intensity in [('DAPI', 8000), ('PANCK', 4000), ('SMA', 2000)]:
    with open(OUT_DIR / f'sample_{ch_name}_intensity.csv', 'w') as f:
        f.write(f'label,{ch_name}\n')
        for i in range(1, 21):
            val = round(base_intensity + np.random.uniform(-1000, 3000), 1)
            f.write(f'{i},{val}\n')
    print(f"  Created sample_{ch_name}_intensity.csv (20 cells, intensity only)")

# 7d. Empty samplesheet (header only, for validation tests)
with open(OUT_DIR / 'empty_samplesheet.csv', 'w') as f:
    f.write('patient_id,path_to_file,is_reference,channels\n')
print("  Created empty_samplesheet.csv")

# 7e. Single sample samplesheet (for single-sample tests)
with open(OUT_DIR / 'single_sample.csv', 'w') as f:
    f.write('patient_id,path_to_file,is_reference,channels\n')
    f.write(f'P001,{TESTDATA_ABS}/P001_ref.ome.tiff,true,DAPI|PANCK|SMA\n')
print("  Created single_sample.csv")

# =============================================================================
# 8. Golden reference files in tests/testdata/expected/
# =============================================================================
print("\n8. Creating golden reference files in expected/...")

# 8a. Expected channel list
with open(EXPECTED_DIR / 'channels_3ch.txt', 'w') as f:
    f.write('DAPI\nPANCK\nSMA\n')
print("  Created expected/channels_3ch.txt")

# 8b. Expected dimensions output
with open(EXPECTED_DIR / 'dims_128x128.txt', 'w') as f:
    f.write('P001_ref.ome.tiff 128 128\n')
print("  Created expected/dims_128x128.txt")

# 8c. Expected max dimensions
with open(EXPECTED_DIR / 'max_dims_128.txt', 'w') as f:
    f.write('MAX_HEIGHT 128\nMAX_WIDTH 128\n')
print("  Created expected/max_dims_128.txt")

# 8d. Expected merged quant columns (fov + cell_size + morphology + markers)
with open(EXPECTED_DIR / 'merged_quant_columns.txt', 'w') as f:
    f.write('fov,cell_size,label,y,x,area,eccentricity,perimeter,convex_area,axis_major_length,axis_minor_length,DAPI,PANCK,SMA\n')
print("  Created expected/merged_quant_columns.txt")

# 8e. Expected morphology columns
with open(EXPECTED_DIR / 'morphology_columns.txt', 'w') as f:
    f.write(','.join(morphology_columns) + '\n')
print("  Created expected/morphology_columns.txt")

# 8f. Expected intensity CSV columns (template — substitute channel name)
with open(EXPECTED_DIR / 'intensity_csv_columns.txt', 'w') as f:
    f.write('label,{CHANNEL}\n')
print("  Created expected/intensity_csv_columns.txt")

# 8g. Expected preprocessing checkpoint columns
with open(EXPECTED_DIR / 'preproc_checkpoint_columns.txt', 'w') as f:
    f.write('patient_id,preprocessed_image,is_reference,channels\n')
print("  Created expected/preproc_checkpoint_columns.txt")

# 8h. Expected registration checkpoint columns
with open(EXPECTED_DIR / 'reg_checkpoint_columns.txt', 'w') as f:
    f.write('patient_id,registered_image,is_reference,channels\n')
print("  Created expected/reg_checkpoint_columns.txt")

print("\n" + "=" * 70)
print("All test data generation complete!")
print("=" * 70)

print("\nThese files can be used to test:")
print("  1. Full pipeline execution with -profile test")
print("  2. Individual process testing with nf-test")
print("  3. Input validation and error handling")
print("  4. Module-level unit tests")
print("  5. Golden file comparison for output correctness")
