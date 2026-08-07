/*
 * MERGE_QUANT_CSVS - Merge per-channel quantification into one table
 *
 * Joins the per-channel marker intensity CSVs (from QUANTIFY) onto the
 * morphology CSV (from EXTRACT_CELL_PROPERTIES) by cell label, validating
 * that no cells are lost or duplicated during the merge.
 *
 * Input: Per-channel intensity CSVs and a morphology CSV
 * Output: Single merged CSV with morphology + per-marker intensities
 */
process MERGE_QUANT_CSVS {
    tag "${meta.patient_id}"
    label 'process_low'

    container "bolt3x/attend_image_analysis:quantification_gpu"

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

    ${ProcessEnvelope.versions(task.process, ['pandas'])}
    """

    stub:
    """
    touch merged_quant.csv
    echo "STUB,${meta.patient_id},stub,0" > ${meta.patient_id}.MERGE_QUANT_CSVS.size.csv

    ${ProcessEnvelope.versionsStub(task.process, ['pandas'])}
    """
}
