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

    container "bolt3x/mirage-quantify:1.0.0"

    input:
    tuple val(meta), path(channel_tiff), path(cell_mask), path(nuclei_mask)

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
    // Per-compartment quantification: route the nuclear mask in when enabled.
    // The mask path is only available here (not in modules.config ext.args), so the
    // toggle is read from params; the --expanded flag arrives via ext.args.
    def nuclei_arg = params.quantify_compartments ? "--nuclei_mask_file ${nuclei_mask}" : ''
    """
    # Log input sizes for tracing (sum of channel_tiff + cell_mask, -L follows symlinks)
    tiff_bytes=\$(stat -L --printf="%s" ${channel_tiff} 2>/dev/null || echo 0)
    mask_bytes=\$(stat -L --printf="%s" ${cell_mask} 2>/dev/null || echo 0)
    total_bytes=\$((tiff_bytes + mask_bytes))
    echo "${task.process},${meta.patient_id},${channel_tiff.name}+${cell_mask.name},\${total_bytes}" > ${meta.id}.QUANTIFY.size.csv

    echo "Sample: ${meta.patient_id}"
    echo "Channel: ${channel_name}"

    # Run quantification on this single channel TIFF
    quantify.py \\
        --channel_tiff ${channel_tiff} \\
        --channel-name '${channel_name}' \\
        --mask_file ${cell_mask} \\
        ${nuclei_arg} \\
        --outdir . \\
        --output_file ${meta.id}_quant.csv \\
        ${args}

    ${ProcessEnvelope.versions(task.process, ['pandas', 'skimage'])}
    """

    stub:
    """
    touch ${meta.id}_quant.csv
    echo "STUB,${meta.id},stub,0" > ${meta.id}.QUANTIFY.size.csv

    ${ProcessEnvelope.versionsStub(task.process, ['pandas', 'skimage'])}
    """
}
