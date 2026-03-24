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
    label 'process_medium'

    container 'bolt3x/attend_image_analysis:quantification_gpu'

    input:
    tuple val(meta), path(quant_csv), path(contours_json)

    output:
    tuple val(meta), path("export/cells.geojson"), emit: geojson
    tuple val(meta), path("export/cells_data.csv"), emit: csv
    path "versions.yml"                            , emit: versions
    path("*.size.csv")                             , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
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
    touch export/cells_data.csv
    echo "STUB,${meta.patient_id},stub,0" > ${meta.patient_id}.EXPORT_GEOJSON.size.csv

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        pandas: stub
        scipy: stub
    END_VERSIONS
    """
}
