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

    container "bolt3x/mirage-quantify:1.0.0"

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
    ${ProcessEnvelope.sizeLog(task.process, meta.patient_id, ["${cell_mask}"], "${meta.patient_id}.EXTRACT_CELL_PROPERTIES.size.csv")}

    echo "Sample: ${meta.patient_id}"

    extract_cell_properties.py \\
        --mask_file ${cell_mask} \\
        --outdir . \\
        ${args}

    ${ProcessEnvelope.versions(task.process, ['skimage'], task.container)}
    """

    stub:
    """
    touch morphology.csv contours.json
    ${ProcessEnvelope.sizeLogStub(task.process, meta.patient_id, "${meta.patient_id}.EXTRACT_CELL_PROPERTIES.size.csv")}

    ${ProcessEnvelope.versionsStub(task.process, ['skimage'], task.container)}
    """
}
