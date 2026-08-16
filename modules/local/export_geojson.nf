/*
 * EXPORT_GEOJSON - Export cell data to QuPath-compatible GeoJSON
 *
 * Exports all cells with raw marker intensities and morphological measurements
 * in QuPath's native GeoJSON format. No phenotype classification is applied —
 * gating is handled downstream by FlowPath in QuPath.
 *
 * Input:
 *   - Merged quantification CSV with per-cell marker intensities + morphology
 *   - Pre-computed contours JSON for polygon cell boundaries
 * Output:
 *   - GeoJSON with QuPath-native measurement format (array of name/value)
 *   - CSV with raw intensities + z-scores per marker
 */
process EXPORT_GEOJSON {
    tag "${meta.patient_id}"

    container "bolt3x/mirage-quantify:1.0.0"

    input:
    // Stage the nucleus contours under a distinct name: both EXTRACT_CELL_PROPERTIES
    // and EXTRACT_NUCLEI_PROPERTIES emit a file literally named contours.json, and
    // (when compartments are disabled) the same cell-contours file is passed into
    // both slots — either way an unstaged duplicate would collide in the work dir.
    tuple val(meta), path(quant_csv), path(contours_json), path(nucleus_contours_json, stageAs: 'nucleus_contours.json')

    output:
    tuple val(meta), path("export/cells.geojson"), emit: geojson
    // Whole-cell-only companion (no nucleusGeometry), written only in the
    // per-compartment path (params.quantify_compartments). Lighter/faster to import
    // in QuPath; same measurements, so FlowPath compartment gating still works.
    tuple val(meta), path("export/cells_wholecell.geojson"), optional: true, emit: geojson_wholecell
    tuple val(meta), path("export/cells_data.csv"), emit: csv
    path "versions.yml"                            , emit: versions
    path("*.size.csv")                             , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    // Per-compartment quantification: pass the nucleus contours (re-keyed to cell
    // labels) so each cell gets a nucleusGeometry in the single combined cells.geojson.
    def nucleus_arg = params.quantify_compartments ? "--nucleus_contours_json ${nucleus_contours_json}" : ''
    """
    # Log input size for tracing (-L follows symlinks)
    input_bytes=\$(stat -L --printf="%s" ${quant_csv} 2>/dev/null || echo 0)
    echo "${task.process},${meta.patient_id},${quant_csv.name},\${input_bytes}" > ${meta.patient_id}.EXPORT_GEOJSON.size.csv

    echo "Sample: ${meta.patient_id}"

    mkdir -p export
    export_geojson.py \\
        --cell_data ${quant_csv} \\
        -o export \\
        --contours_json ${contours_json} \\
        ${nucleus_arg} \\
        ${args}

    ${ProcessEnvelope.versions(task.process, ['pandas', 'scipy'])}
    """

    stub:
    """
    mkdir -p export
    touch export/cells.geojson
    ${params.quantify_compartments ? 'touch export/cells_wholecell.geojson' : ''}
    touch export/cells_data.csv
    echo "STUB,${meta.patient_id},stub,0" > ${meta.patient_id}.EXPORT_GEOJSON.size.csv

    ${ProcessEnvelope.versionsStub(task.process, ['pandas', 'scipy'])}
    """
}
