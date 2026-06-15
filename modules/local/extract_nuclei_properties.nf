/*
 * EXTRACT_NUCLEI_PROPERTIES - Compute nucleus contours, re-keyed to cell labels
 *
 * Runs (only when params.quantify_compartments) on the nuclear mask, passing the
 * whole-cell mask as --reference_mask so the emitted contours.json is keyed by
 * CELL label. EXPORT_GEOJSON then attaches each nucleus polygon to its cell via a
 * plain identity lookup. Re-uses extract_cell_properties.py.
 *
 * Input:  nuclei mask + cell mask (same patient)
 * Output: morphology.csv (nucleus, unused downstream) + contours.json (keyed by cell label)
 */
process EXTRACT_NUCLEI_PROPERTIES {
    tag "${meta.patient_id}"
    label 'process_medium'

    container "bolt3x/attend_image_analysis:quantification_gpu"

    input:
    tuple val(meta), path(nuclei_mask), path(cell_mask)

    output:
    tuple val(meta), path("morphology.csv") , emit: morphology
    tuple val(meta), path("contours.json")  , emit: contours
    path "versions.yml"                      , emit: versions
    path("*.size.csv")                       , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    """
    # Log input size for tracing (-L follows symlinks)
    input_bytes=\$(stat -L --printf="%s" ${nuclei_mask} 2>/dev/null || echo 0)
    echo "${task.process},${meta.patient_id},${nuclei_mask.name},\${input_bytes}" > ${meta.patient_id}.EXTRACT_NUCLEI_PROPERTIES.size.csv

    echo "Sample: ${meta.patient_id}"

    extract_cell_properties.py \\
        --mask_file ${nuclei_mask} \\
        --reference_mask ${cell_mask} \\
        --outdir . \\
        ${args}

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
        scikit-image: \$(python -c "import skimage; print(skimage.__version__)" 2>/dev/null || echo "unknown")
    END_VERSIONS
    """

    stub:
    """
    touch morphology.csv contours.json
    echo "STUB,${meta.patient_id},stub,0" > ${meta.patient_id}.EXTRACT_NUCLEI_PROPERTIES.size.csv

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        scikit-image: stub
    END_VERSIONS
    """
}
