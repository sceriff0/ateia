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

    container "bolt3x/attend_image_analysis:quantification_gpu"

    input:
    // Stage the nucleus contours under a distinct name: both EXTRACT_CELL_PROPERTIES
    // and EXTRACT_NUCLEI_PROPERTIES emit a file literally named contours.json, and
    // (when compartments are disabled) the same cell-contours file is passed into
    // both slots — either way an unstaged duplicate would collide in the work dir.
    tuple val(meta), path(quant_csv), path(contours_json), path(nucleus_contours_json, stageAs: 'nucleus_contours.json'), path(phenotypes, stageAs: 'phenotypes.csv'), path(model_config, stageAs: 'model_config.json')

    output:
    tuple val(meta), path("export/cells.geojson"), emit: geojson
    // Whole-cell-only companion (no nucleusGeometry), written only in the
    // per-compartment path (params.quantify_compartments). Lighter/faster to import
    // in QuPath; same measurements, so FlowPath compartment gating still works.
    tuple val(meta), path("export/cells_wholecell.geojson"), optional: true, emit: geojson_wholecell
    tuple val(meta), path("export/cells_data.csv"), emit: csv
    tuple val(meta), path("export/panel_model.json"), optional: true, emit: panel_model
    path "versions.yml"                            , emit: versions
    path("*.size.csv")                             , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    // Per-compartment quantification: pass the nucleus contours (re-keyed to cell
    // labels) so each cell gets a nucleusGeometry in the single combined cells.geojson.
    def nucleus_arg = params.quantify_compartments ? "--nucleus_contours_json ${nucleus_contours_json}" : ''
    // Phenotype-aware export: only wired up when a panel is configured. Otherwise
    // export_geojson.py falls back to its legacy constant-"Cell" classification and
    // writes no panel_model.json sidecar.
    def pheno_arg = (params.panel_spec || params.panel_model) ? "--phenotypes ${phenotypes} --panel_model ${model_config} --patient_id ${meta.patient_id}" : ''
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
        ${pheno_arg} \\
        ${args}

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
        pandas: \$(python -c "import pandas; print(pandas.__version__)" 2>/dev/null || echo "unknown")
        scipy: \$(python -c "import scipy; print(scipy.__version__)" 2>/dev/null || echo "unknown")
    END_VERSIONS
    """

    stub:
    """
    mkdir -p export
    touch export/cells.geojson
    ${params.quantify_compartments ? 'touch export/cells_wholecell.geojson' : ''}
    touch export/cells_data.csv
    ${(params.panel_spec || params.panel_model) ? 'touch export/panel_model.json' : ''}
    echo "STUB,${meta.patient_id},stub,0" > ${meta.patient_id}.EXPORT_GEOJSON.size.csv

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        pandas: stub
        scipy: stub
    END_VERSIONS
    """
}
