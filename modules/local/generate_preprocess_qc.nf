/*
 * GENERATE_PREPROCESS_QC - render per-channel preprocessing QC thumbnails.
 */
process GENERATE_PREPROCESS_QC {
    tag "${meta.patient_id}"
    label 'process_medium'

    container "bolt3x/attend_image_analysis:preprocess"

    input:
    tuple val(meta), path(preprocessed)

    output:
    tuple val(meta), path("qc/*.png", optional: true), emit: qc
    path "versions.yml"              , emit: versions
    path("*.size.csv")               , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    def prefix = task.ext.prefix ?: "${preprocessed.simpleName}"
    def scale_factor = params.preprocess_qc_scale_factor
    def channels = meta.channels.collect { "\"${it}\"" }.join(' ')
    """
    # Log input size for tracing (-L follows symlinks)
    input_bytes=\$(stat -L --printf="%s" ${preprocessed} 2>/dev/null || echo 0)
    echo "${task.process},${meta.patient_id},${preprocessed.name},\${input_bytes}" > ${meta.patient_id}_${preprocessed.simpleName}.GENERATE_PREPROCESS_QC.size.csv

    mkdir -p qc

    generate_preprocess_qc.py \\
        --image ${preprocessed} \\
        --output qc \\
        --channels ${channels} \\
        --scale-factor ${scale_factor} \\
        --prefix ${prefix} \\
        ${args}

    ${ProcessEnvelope.versions(task.process, ['numpy', 'tifffile', 'skimage'])}
    """

    stub:
    def prefix = task.ext.prefix ?: "${preprocessed.simpleName}"
    """
    mkdir -p qc
    touch qc/${prefix}_DAPI.png
    touch qc/${prefix}_channel1.png
    echo "STUB,${meta.patient_id},stub,0" > ${meta.patient_id}_${preprocessed.simpleName}.GENERATE_PREPROCESS_QC.size.csv

    ${ProcessEnvelope.versionsStub(task.process, ['numpy', 'tifffile', 'skimage'])}
    """
}
