/*
 * SPLIT_CHANNELS - Split multi-channel TIFF into individual channels
 *
 * Extracts individual channel images from multi-channel OME-TIFFs for
 * per-channel processing. WHICH channels are emitted is decided once per slide at
 * samplesheet read by CsvUtils.resolveKeptChannelsPerSlide and carried here as
 * meta.keep_channels; this process applies no nuclear-marker rule of its own.
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
    // This process performs NO nuclear reasoning. The keep-set was resolved once per
    // slide at samplesheet read (CsvUtils.resolveKeptChannelsPerSlide) and travels on
    // the meta map, so the process just emits what it was handed. That is what makes
    // CsvUtils.countChannelsPerPatient's groupKey size exact by construction, rather
    // than by three copies of one rule continuing to agree by hand.
    //
    // Empty only for SPLIT_PRIOR_PYRAMID, which reads channel names from OME-XML at
    // runtime; omitting the flag makes split_multichannel.py fall back to its
    // is_reference rule, and that caller is is_reference=true, i.e. keep everything.
    def keep_args = (meta.keep_channels && !meta.keep_channels.isEmpty()) ?
        "--keep-channels ${meta.keep_channels.join(' ')}" : ""
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
        ${keep_args} \\
        --pixel-size ${params.pixel_size} \\
        ${args}

    ${ProcessEnvelope.versions(task.process, ['tifffile', 'numpy'])}
    """

    stub:
    // Exactly what the real split emits, from the same source of truth: the keep-set
    // resolved at samplesheet read. CsvUtils.countChannelsPerPatient sizes the
    // postprocessing groupKeys from that SAME resolver, so the stub's output count
    // matches the size the group expects.
    def out_channels = meta.keep_channels ?: meta.channels
    // When meta.channels is empty (e.g. ADD_CYCLE's SPLIT_PRIOR_PYRAMID, which
    // reads channel names from OME-XML in REAL mode only), still emit a single
    // placeholder so the mandatory `*.tiff` output binds under -stub.
    def touch_cmds = out_channels ? out_channels.collect { "touch ${it}.tiff" }.join('\n    ') : "touch prior_pyramid_channel.tiff"
    """
    # One stub file per channel in the keep-set, which is what the real split emits.
    ${touch_cmds}

    echo "STUB,${meta.patient_id},stub,0" > ${meta.patient_id}_${registered_image.simpleName}.SPLIT_CHANNELS.size.csv

    ${ProcessEnvelope.versionsStub(task.process, ['tifffile', 'numpy'])}
    """
}
