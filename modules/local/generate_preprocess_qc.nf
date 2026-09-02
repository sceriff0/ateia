/*
 * GENERATE_PREPROCESS_QC - render per-channel preprocessing QC thumbnails.
 */
process GENERATE_PREPROCESS_QC {
    tag "${meta.patient_id}"
    label 'process_medium'

    container "bolt3x/mirage-preprocess:1.0.0"

    input:
    tuple val(meta), path(preprocessed)

    output:
    tuple val(meta), path("qc/*.png"), emit: qc, optional: true
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
    ${ProcessEnvelope.sizeLog(task.process, meta.patient_id, ["${preprocessed}"], "${meta.patient_id}_${preprocessed.simpleName}.GENERATE_PREPROCESS_QC.size.csv")}

    mkdir -p qc

    generate_preprocess_qc.py \\
        --image ${preprocessed} \\
        --output qc \\
        --channels ${channels} \\
        --scale-factor ${scale_factor} \\
        --prefix ${prefix} \\
        ${args}

    ${ProcessEnvelope.versions(task.process, ['numpy', 'tifffile', 'skimage'], task.container)}
    """

    stub:
    def prefix = task.ext.prefix ?: "${preprocessed.simpleName}"
    """
    mkdir -p qc
    touch qc/${prefix}_DAPI.png
    touch qc/${prefix}_channel1.png
    ${ProcessEnvelope.sizeLogStub(task.process, meta.patient_id, "${meta.patient_id}_${preprocessed.simpleName}.GENERATE_PREPROCESS_QC.size.csv")}

    ${ProcessEnvelope.versionsStub(task.process, ['numpy', 'tifffile', 'skimage'], task.container)}
    """
}
