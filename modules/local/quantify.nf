nextflow.enable.dsl = 2

/*
 * QUANTIFY - Marker intensity quantification
 *
 * Measures per-cell marker intensities from single-channel TIFFs using
 * segmentation masks. Computes morphological features and intensity statistics.
 *
 * Input: Single-channel TIFF and segmentation mask
 * Output: Per-channel quantification CSV with cell measurements
 */
process QUANTIFY {
    tag "${meta.patient_id} - ${meta.channel_name}"
    label 'process_low'

    container 'bolt3x/attend_image_analysis:quantification_gpu'

    input:
    tuple val(meta), path(channel_tiff), path(seg_mask)

    output:
    tuple val(meta), path("${meta.id}_quant.csv"), emit: individual_csv
    path "versions.yml"                           , emit: versions
    path("*.size.csv")                            , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    def prefix = task.ext.prefix ?: "${meta.patient_id}"
    // Channel name from CSV metadata (set in postprocess.nf from meta.channels)
    def channel_name = meta.channel_name
    """
    # Log input sizes for tracing (sum of channel_tiff + seg_mask, -L follows symlinks)
    tiff_bytes=\$(stat -L --printf="%s" ${channel_tiff} 2>/dev/null || echo 0)
    mask_bytes=\$(stat -L --printf="%s" ${seg_mask} 2>/dev/null || echo 0)
    total_bytes=\$((tiff_bytes + mask_bytes))
    echo "${task.process},${meta.id},${channel_tiff.name}+${seg_mask.name},\${total_bytes}" > ${meta.id}.QUANTIFY.size.csv

    echo "Sample: ${meta.patient_id}"
    echo "Channel: ${channel_name}"

    # Run quantification on this single channel TIFF
    quantify.py \\
        --channel_tiff ${channel_tiff} \\
        --channel-name ${channel_name} \\
        --mask_file ${seg_mask} \\
        --outdir . \\
        --output_file ${meta.id}_quant.csv \\
        --min_area ${params.quant_min_area} \\
        ${args}

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
        pandas: \$(python -c "import pandas; print(pandas.__version__)" 2>/dev/null || echo "unknown")
        scikit-image: \$(python -c "import skimage; print(skimage.__version__)" 2>/dev/null || echo "unknown")
    END_VERSIONS
    """

    stub:
    def prefix = task.ext.prefix ?: "${meta.patient_id}"
    """
    touch ${meta.id}_quant.csv
    echo "STUB,${meta.id},stub,0" > ${meta.id}.QUANTIFY.size.csv

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        pandas: stub
        scikit-image: stub
    END_VERSIONS
    """
}

process MERGE_QUANT_CSVS {
    tag "${meta.patient_id}"
    label 'process_low'

    container 'bolt3x/attend_image_analysis:quantification_gpu'

    input:
    tuple val(meta), path(individual_csvs), path(morphology_csv)

    output:
    tuple val(meta), path("merged_quant.csv"), emit: merged_csv
    path "versions.yml"                       , emit: versions
    path("*.size.csv")                        , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    def prefix = task.ext.prefix ?: "${meta.patient_id}"
    """
    #!/usr/bin/env python3
    import pandas as pd
    from pathlib import Path
    import sys
    import os

    # Log input size for tracing (sum of all CSV files + morphology)
    csv_files = sorted(Path('.').glob('*_quant.csv'))
    morphology_size = Path('${morphology_csv}').stat().st_size
    total_bytes = sum(f.stat().st_size for f in csv_files) + morphology_size
    with open('${meta.patient_id}.MERGE_QUANT_CSVS.size.csv', 'w') as f:
        f.write(f"${task.process},${meta.patient_id},csvs/,{total_bytes}\\n")

    print("Sample: ${meta.patient_id}")

    # Load morphology (computed once by EXTRACT_CELL_PROPERTIES)
    morphology = pd.read_csv('${morphology_csv}')
    print(f"Morphology: {len(morphology)} cells, {len(morphology.columns)} columns")

    # Load all intensity CSVs (each has only: label, <marker>)
    csv_files = sorted(Path('.').glob('*_quant.csv'))

    if not csv_files:
        print("ERROR: No quantification CSVs found", file=sys.stderr)
        sys.exit(1)

    print(f"Merging {len(csv_files)} intensity CSVs with morphology...")

    # Start with morphology as the base table
    merged = morphology.copy()
    morphology_cells = set(merged['label'])

    # Merge each intensity CSV by label (left join on morphology)
    for csv_file in csv_files:
        df = pd.read_csv(csv_file)
        marker_cols = [col for col in df.columns if col != 'label']

        if not marker_cols:
            print(f"  WARNING: {csv_file.name}: No marker columns found, skipping")
            continue

        # Validate cell labels
        intensity_cells = set(df['label'])
        missing = morphology_cells - intensity_cells
        extra = intensity_cells - morphology_cells

        if missing:
            print(f"  WARNING: {csv_file.name}: Missing {len(missing)} cells from morphology")
        if extra:
            print(f"  WARNING: {csv_file.name}: Has {len(extra)} extra cells (will be ignored)")

        merge_df = df[['label'] + marker_cols]
        merged = merged.merge(merge_df, on='label', how='left')

        for col in marker_cols:
            merged[col] = merged[col].fillna(0.0)

        print(f"  + {', '.join(marker_cols)} from {csv_file.name}")

    # Validate no cells were lost
    cells_lost = len(morphology) - len(merged)
    if cells_lost > 0:
        print(f"\\nCRITICAL ERROR: Lost {cells_lost} cells during merge", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"\\nAll {len(merged)} cells preserved")

    # Reorder columns: morphology first, then DAPI, then other markers sorted
    morphology_cols = ['label', 'y', 'x', 'area', 'eccentricity', 'perimeter',
                      'convex_area', 'axis_major_length', 'axis_minor_length']
    morpho_present = [col for col in morphology_cols if col in merged.columns]
    marker_cols_all = [col for col in merged.columns if col not in morphology_cols]

    # Put DAPI first among markers if present
    if 'DAPI' in marker_cols_all:
        marker_cols_all.remove('DAPI')
        final_column_order = morpho_present + ['DAPI'] + sorted(marker_cols_all)
    else:
        final_column_order = morpho_present + sorted(marker_cols_all)
    merged = merged[final_column_order]

    # Add required columns for Pixie cell clustering
    merged['fov'] = '${meta.patient_id}'
    if 'area' in merged.columns:
        merged['cell_size'] = merged['area']

    # Update column order to put fov first, then cell_size near the beginning
    cols = merged.columns.tolist()
    for col_to_move in ['cell_size', 'fov']:
        if col_to_move in cols:
            cols.remove(col_to_move)
            cols = [col_to_move] + cols
    merged = merged[cols]

    # Save merged CSV
    merged.to_csv('merged_quant.csv', index=False)
    print(f"\\nMerged CSV saved: {len(merged)} cells, {len(merged.columns)} columns")
    print(f"  Final columns: {', '.join(merged.columns)}")

    # Write versions file
    with open('versions.yml', 'w') as f:
        f.write('"${task.process}":\\n')
        f.write(f'    python: {sys.version.split()[0]}\\n')
        f.write(f'    pandas: {pd.__version__}\\n')
    """

    stub:
    def prefix = task.ext.prefix ?: "${meta.patient_id}"
    """
    touch merged_quant.csv
    echo "STUB,${meta.patient_id},stub,0" > ${meta.patient_id}.MERGE_QUANT_CSVS.size.csv

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        pandas: stub
    END_VERSIONS
    """
}
