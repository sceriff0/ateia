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

    container "bolt3x/mirage-quantify:1.0.0"

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
    // Which column is the nuclear/fiducial marker decides two things in the merge:
    // it is the one protected from being overwritten on an add_cycle run, and it
    // leads the marker block. Both were pinned to the literal 'DAPI'. MarkerUtils
    // is the one sanctioned reader of params.nuclear_markers -- it normalises the
    // bare-String and comma-joined shapes that fail SILENTLY when read raw
    // (tests/test_nuclear_marker_routing.py).
    def nuclear_args = "--nuclear-markers ${MarkerUtils.markerList(params.nuclear_markers).join(' ')}"
    """
    ${ProcessEnvelope.sizeLog(task.process, meta.patient_id, ['*_quant.csv', "${morphology_csv}"], "${meta.patient_id}.MERGE_QUANT_CSVS.size.csv")}

    merge_quant_csvs.py \\
        --csv-files ${individual_csvs} \\
        --morphology ${morphology_csv} \\
        --patient-id ${meta.patient_id} \\
        --output merged_quant.csv \\
        ${nuclear_args} \\
        ${args}

    ${ProcessEnvelope.versions(task.process, ['pandas'], task.container)}
    """

    stub:
    """
    touch merged_quant.csv
    ${ProcessEnvelope.sizeLogStub(task.process, meta.patient_id, "${meta.patient_id}.MERGE_QUANT_CSVS.size.csv")}

    ${ProcessEnvelope.versionsStub(task.process, ['pandas'], task.container)}
    """
}
