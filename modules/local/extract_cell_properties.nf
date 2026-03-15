/*
 * EXTRACT_CELL_PROPERTIES - Compute morphology and contours from cell mask
 *
 * Runs ONCE per patient (after SEGMENT) to extract:
 *   - Morphological properties via regionprops (centroids, area, shape descriptors)
 *   - Simplified polygon contours via find_contours + Douglas-Peucker
 *
 * This avoids redundant regionprops computation in per-channel QUANTIFY processes
 * and provides contours for polygon-based GeoJSON export in PHENOTYPE.
 *
 * Input: Cell segmentation mask from SEGMENT
 * Output: morphology.csv + contours.json
 */
process EXTRACT_CELL_PROPERTIES {
    tag "${meta.patient_id}"
    label 'process_medium'

    container 'bolt3x/attend_image_analysis:quantification_gpu'

    input:
    tuple val(meta), path(cell_mask)

    output:
    tuple val(meta), path("morphology.csv") , emit: morphology
    tuple val(meta), path("contours.json")  , emit: contours
    path "versions.yml"                      , emit: versions
    path("*.size.csv")                       , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    def prefix = task.ext.prefix ?: "${meta.patient_id}"
    """
    # Log input size for tracing (-L follows symlinks)
    input_bytes=\$(stat -L --printf="%s" ${cell_mask} 2>/dev/null || echo 0)
    echo "${task.process},${meta.patient_id},${cell_mask.name},\${input_bytes}" > ${meta.patient_id}.EXTRACT_CELL_PROPERTIES.size.csv

    echo "Sample: ${meta.patient_id}"

    extract_cell_properties.py \\
        --mask_file ${cell_mask} \\
        --outdir . \\
        --min_area ${params.quant_min_area} \\
        ${args}

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
        scikit-image: \$(python -c "import skimage; print(skimage.__version__)" 2>/dev/null || echo "unknown")
    END_VERSIONS
    """

    stub:
    def prefix = task.ext.prefix ?: "${meta.patient_id}"
    """
    touch morphology.csv contours.json
    echo "STUB,${meta.patient_id},stub,0" > ${meta.patient_id}.EXTRACT_CELL_PROPERTIES.size.csv

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        scikit-image: stub
    END_VERSIONS
    """
}
