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

    container "bolt3x/attend_image_analysis:quantification_gpu"

    input:
    tuple val(meta), path(channel_tiff), path(cell_mask), path(nuclei_mask), path(redsea_npz)

    output:
    tuple val(meta), path("${meta.id}_quant.csv"), emit: individual_csv
    path "versions.yml"                           , emit: versions
    path("*.size.csv")                            , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    // The marker's DECLARED name, resolved from meta.channels by
    // quantify_markers.nf's ChannelName.declaredFor -- NOT the tiff's filename
    // stem. It fills the <marker> slot of the "<marker>: <Compartment>:
    // <Statistic>" key qupath-extension-flowpath parses case-sensitively, so it
    // must arrive at bin/quantify.py byte-for-byte as the samplesheet spelled it.
    //
    // Which is why it is POSIX-quoted rather than wrapped in hand-written single
    // quotes: a declared name is arbitrary samplesheet text, and until this seam
    // existed the value reaching here had already been through a filename
    // allowlist, so nothing unquotable could occur.
    def channel_name = meta.channel_name
    def channel_arg = ChannelName.shellQuote(channel_name)
    // Per-compartment quantification: route the nuclear mask in when enabled.
    // The mask path is only available here (not in modules.config ext.args), so the
    // toggle is read from params; the --expanded flag arrives via ext.args.
    def nuclei_arg = params.quantify_compartments ? "--nuclei_mask_file ${nuclei_mask}" : ''
    // REDSEA geometry is a required input so this process has ONE input arity
    // regardless of --redsea; when the feature is off the staged file is
    // assets/NO_REDSEA. Gating on the filename rather than on params keeps this
    // `script:` block from growing a second `params` reference (a bare `params`
    // here hashes the whole map -- see CLAUDE.md, "Verification reality"); the
    // marker list and REDSEAChecker arrive through ext.args instead.
    def redsea_arg = redsea_npz.name == 'NO_REDSEA' ? '' : "--redsea-geometry ${redsea_npz}"
    """
    # Log input sizes for tracing (sum of channel_tiff + cell_mask, -L follows symlinks)
    tiff_bytes=\$(stat -L --printf="%s" ${channel_tiff} 2>/dev/null || echo 0)
    mask_bytes=\$(stat -L --printf="%s" ${cell_mask} 2>/dev/null || echo 0)
    total_bytes=\$((tiff_bytes + mask_bytes))
    echo "${task.process},${meta.patient_id},${channel_tiff.name}+${cell_mask.name},\${total_bytes}" > ${meta.id}.QUANTIFY.size.csv

    echo "Sample: ${meta.patient_id}"
    echo "Channel: "${channel_arg}

    # Run quantification on this single channel TIFF
    quantify.py \\
        --channel_tiff ${channel_tiff} \\
        --channel-name ${channel_arg} \\
        --mask_file ${cell_mask} \\
        ${nuclei_arg} \\
        --outdir . \\
        --output_file ${meta.id}_quant.csv \\
        ${redsea_arg} \\
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
