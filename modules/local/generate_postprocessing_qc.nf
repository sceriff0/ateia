/*
 * GENERATE_POSTPROCESSING_QC - render segmentation and quantification QC figures.
 */
process GENERATE_POSTPROCESSING_QC {
    tag "${meta.patient_id}"
    label 'process_medium'

    container "bolt3x/mirage-quantify:1.0.0"

    input:
    tuple val(meta), path(cell_mask), path(merged_csv)

    output:
    tuple val(meta), path("qc/*.png"), emit: qc, optional: true
    path "versions.yml"              , emit: versions
    path("*.size.csv")               , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    def prefix = task.ext.prefix ?: "${meta.patient_id}"
    """
    ${ProcessEnvelope.sizeLog(task.process, meta.patient_id, ["${cell_mask}", "${merged_csv}"], "${meta.patient_id}.GENERATE_POSTPROCESSING_QC.size.csv")}

    mkdir -p qc

    generate_postprocessing_qc.py \\
        --mask ${cell_mask} \\
        --csv ${merged_csv} \\
        --output qc \\
        --prefix ${prefix} \\
        ${args}

    ${ProcessEnvelope.versions(task.process, ['numpy', 'pandas', 'matplotlib', 'skimage'], task.container)}
    """

    stub:
    def prefix = task.ext.prefix ?: "${meta.patient_id}"
    """
    mkdir -p qc
    touch qc/${prefix}_seg_overlay.png
    touch qc/${prefix}_cell_stats.png
    touch qc/${prefix}_intensity_distributions.png
    ${ProcessEnvelope.sizeLogStub(task.process, meta.patient_id, "${meta.patient_id}.GENERATE_POSTPROCESSING_QC.size.csv")}

    ${ProcessEnvelope.versionsStub(task.process, ['numpy', 'pandas', 'matplotlib', 'skimage'], task.container)}
    """
}
