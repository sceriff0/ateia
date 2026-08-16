/*
 * SPLIT_CHANNELS - Split multi-channel TIFF into individual channels
 *
 * Extracts individual channel images from multi-channel OME-TIFFs for
 * per-channel processing. The nuclear/fiducial channel (params.nuclear_markers,
 * via MarkerUtils) is kept from the reference image only.
 *
 * Input: Registered multi-channel OME-TIFF and reference flag
 * Output: Individual single-channel TIFF files per marker
 */
process SPLIT_CHANNELS {
    tag "${meta.patient_id}"

    container "bolt3x/mirage-preprocess:1.0.0"

    input:
    tuple val(meta), path(registered_image), val(is_reference)

    output:
    tuple val(meta), path("*.tiff", includeInputs: false), emit: channels
    path "versions.yml"                                   , emit: versions
    path("*.size.csv")                                    , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    def ref_flag = is_reference ? "--is-reference" : ""
    // Pass channel names from metadata if available and valid
    def channel_args = (meta.channels && meta.channels instanceof List && !meta.channels.isEmpty()) ?
        "--channels ${meta.channels.join(' ')}" : ""
    // The REAL drop-the-nuclear-channel decision is made in Python, so the marker list
    // has to travel there — a hardcoded 'DAPI' in split_multichannel.py would keep a
    // CELLTOX channel that CsvUtils.countChannelsPerPatient (same MarkerUtils rule) has
    // already counted as dropped, over-filling the patient's groupTuple.
    // MarkerUtils.markerList also normalises the bare-String form Nextflow produces for
    // `--nuclear_markers CELLTOX`, so CLI and config spellings agree.
    def nuclear_args = "--nuclear-markers ${MarkerUtils.markerList(params.nuclear_markers).join(' ')}"
    """
    # Log input size for tracing (-L follows symlinks)
    input_bytes=\$(stat -L --printf="%s" ${registered_image} 2>/dev/null || echo 0)
    echo "${task.process},${meta.patient_id},${registered_image.name},\${input_bytes}" > ${meta.patient_id}_${registered_image.simpleName}.SPLIT_CHANNELS.size.csv

    echo "Sample: ${meta.patient_id}"
    echo "Channels: ${(meta.channels && meta.channels instanceof List) ? meta.channels.join(', ') : 'Will read from OME metadata'}"

    split_multichannel.py \\
        ${registered_image} \\
        . \\
        ${ref_flag} \\
        ${channel_args} \\
        ${nuclear_args} \\
        --pixel-size ${params.pixel_size} \\
        ${args}

    ${ProcessEnvelope.versions(task.process, ['tifffile', 'numpy'])}
    """

    stub:
    // Same rule as the real split, via the one shared implementation: the nuclear
    // channel survives only on the reference slide. MarkerUtils reads
    // params.nuclear_markers so this no longer hardcodes 'DAPI' — and, critically,
    // CsvUtils.countChannelsPerPatient derives the postprocessing groupKey sizes from
    // the SAME method, so the stub's output count matches the size the group expects.
    def out_channels = MarkerUtils.splitOutputChannels(meta.channels, is_reference, params.nuclear_markers)
    // When meta.channels is empty (e.g. ADD_CYCLE's SPLIT_PRIOR_PYRAMID, which
    // reads channel names from OME-XML in REAL mode only), still emit a single
    // placeholder so the mandatory `*.tiff` output binds under -stub.
    def touch_cmds = out_channels ? out_channels.collect { "touch ${it}.tiff" }.join('\n    ') : "touch prior_pyramid_channel.tiff"
    """
    # One stub file per output channel (the nuclear channel is dropped for
    # non-references, matching the real split which keeps it only on the reference).
    ${touch_cmds}

    echo "STUB,${meta.patient_id},stub,0" > ${meta.patient_id}_${registered_image.simpleName}.SPLIT_CHANNELS.size.csv

    ${ProcessEnvelope.versionsStub(task.process, ['tifffile', 'numpy'])}
    """
}
