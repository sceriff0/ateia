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
    // Channel name from CSV metadata (set in postprocess.nf from meta.channels)
    def channel_name = meta.channel_name
    """
    # Log input sizes for tracing (sum of channel_tiff + seg_mask, -L follows symlinks)
    tiff_bytes=\$(stat -L --printf="%s" ${channel_tiff} 2>/dev/null || echo 0)
    mask_bytes=\$(stat -L --printf="%s" ${seg_mask} 2>/dev/null || echo 0)
    total_bytes=\$((tiff_bytes + mask_bytes))
    echo "${task.process},${meta.patient_id},${channel_tiff.name}+${seg_mask.name},\${total_bytes}" > ${meta.id}.QUANTIFY.size.csv

    echo "Sample: ${meta.patient_id}"
    echo "Channel: ${channel_name}"

    # Run quantification on this single channel TIFF
    quantify.py \\
        --channel_tiff ${channel_tiff} \\
        --channel-name '${channel_name}' \\
        --mask_file ${seg_mask} \\
        --outdir . \\
        --output_file ${meta.id}_quant.csv \\
        ${args}

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
        pandas: \$(python -c "import pandas; print(pandas.__version__)" 2>/dev/null || echo "unknown")
        scikit-image: \$(python -c "import skimage; print(skimage.__version__)" 2>/dev/null || echo "unknown")
    END_VERSIONS
    """

    stub:
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
    """
    # Log input size for tracing
    total_bytes=\$(find . -name '*_quant.csv' -exec stat -L --printf="%s\\n" {} + 2>/dev/null | awk '{sum+=\$1} END {print sum}')
    morph_bytes=\$(stat -L --printf="%s" ${morphology_csv} 2>/dev/null || echo 0)
    total_bytes=\$((total_bytes + morph_bytes))
    echo "${task.process},${meta.patient_id},csvs/,\${total_bytes}" > ${meta.patient_id}.MERGE_QUANT_CSVS.size.csv

    merge_quant_csvs.py \\
        --csv-files ${individual_csvs} \\
        --morphology ${morphology_csv} \\
        --patient-id ${meta.patient_id} \\
        --output merged_quant.csv \\
        ${args}

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
        pandas: \$(python -c "import pandas; print(pandas.__version__)" 2>/dev/null || echo "unknown")
    END_VERSIONS
    """

    stub:
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
