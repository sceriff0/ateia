/*
 * GENERATE_REGISTRATION_QC - render registered-vs-reference RGB overlay QC images.
 *
 * The overlay is built from the nuclear/fiducial channel, resolved per image from its
 * OME channel names against params.nuclear_markers (via MarkerUtils, the same rule
 * SPLIT_CHANNELS and SEGMENT use). Before this was plumbed through, bin/utils/qc.py
 * tested for the literal "DAPI" and fell back to channel 0 -- correct only because
 * CONVERT_IMAGE reserves channel 0 for the nuclear channel, and noisy on every
 * CELLTOX panel.
 */
process GENERATE_REGISTRATION_QC {
    tag "${meta.patient_id}"
    label 'process_high'

    container "bolt3x/attend_image_analysis:debug_diffeo"

    input:
    tuple val(meta), path(registered), path(reference)

    output:
    tuple val(meta), path("qc/*_QC_RGB.{png,tif}"), emit: qc
    tuple val(meta), path("qc/*_QC_RGB_fullres.tif"), optional: true, emit: qc_fullres
    path "versions.yml"                           , emit: versions
    path("*.size.csv")                            , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    def scale_factor = params.qc_scale_factor
    // MarkerUtils.markerList also normalises the bare-String form Nextflow produces for
    // `--nuclear_markers CELLTOX`, so CLI and config spellings agree (see SPLIT_CHANNELS).
    def nuclear_args = "--nuclear-markers ${MarkerUtils.markerList(params.nuclear_markers).join(' ')}"
    """
    # Log input sizes for tracing (sum of registered + reference, -L follows symlinks)
    reg_bytes=\$(stat -L --printf="%s" ${registered} 2>/dev/null || echo 0)
    ref_bytes=\$(stat -L --printf="%s" ${reference} 2>/dev/null || echo 0)
    total_bytes=\$((reg_bytes + ref_bytes))
    echo "${task.process},${meta.patient_id},${registered.name}+${reference.name},\${total_bytes}" > ${meta.patient_id}_${registered.simpleName}.GENERATE_REGISTRATION_QC.size.csv

    mkdir -p qc

    generate_registration_qc.py \\
        --reference ${reference} \\
        --registered ${registered} \\
        --output qc \\
        --scale-factor ${scale_factor} \\
        ${nuclear_args} \\
        ${args}

    ${ProcessEnvelope.versions(task.process, ['numpy', 'tifffile'])}
    """

    stub:
    """
    mkdir -p qc
    touch qc/${registered.simpleName}_QC_RGB.png
    touch qc/${registered.simpleName}_QC_RGB_fullres.tif
    echo "STUB,${meta.patient_id},stub,0" > ${meta.patient_id}_${registered.simpleName}.GENERATE_REGISTRATION_QC.size.csv

    ${ProcessEnvelope.versionsStub(task.process, ['numpy', 'tifffile'])}
    """
}
