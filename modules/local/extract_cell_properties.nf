/*
 * EXTRACT_CELL_PROPERTIES - Compute morphology and contours from cell mask
 *
 * Runs ONCE per patient (after SEGMENT) to extract:
 *   - Morphological properties via regionprops (centroids, area, shape descriptors)
 *   - Simplified polygon contours via find_contours + Douglas-Peucker
 *
 * This avoids redundant regionprops computation in per-channel QUANTIFY processes
 * and provides contours for polygon-based GeoJSON export in EXPORT_GEOJSON.
 *
 * Input: Cell segmentation mask from SEGMENT
 * Output: morphology.csv + contours.json
 */
process EXTRACT_CELL_PROPERTIES {
    tag "${meta.patient_id}"

    container "bolt3x/attend_image_analysis:quantification_gpu"

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
    """
    # Log input size for tracing (-L follows symlinks)
    input_bytes=\$(stat -L --printf="%s" ${cell_mask} 2>/dev/null || echo 0)
    echo "${task.process},${meta.patient_id},${cell_mask.name},\${input_bytes}" > ${meta.patient_id}.EXTRACT_CELL_PROPERTIES.size.csv

    echo "Sample: ${meta.patient_id}"

    extract_cell_properties.py \\
        --mask_file ${cell_mask} \\
        --outdir . \\
        ${args}

    ${ProcessEnvelope.versions(task.process, ['skimage'])}
    """

    stub:
    """
    touch morphology.csv contours.json
    echo "STUB,${meta.patient_id},stub,0" > ${meta.patient_id}.EXTRACT_CELL_PROPERTIES.size.csv

    ${ProcessEnvelope.versionsStub(task.process, ['skimage'])}
    """
}
