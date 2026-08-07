/*
 * EXTRACT_MASK_SERIES - read the second OME series (Image:1) from a
 * mask-carrying pyramid OME-TIFF and emit cell_mask.tif / nuclei_mask.tif.
 * Fast-fails when the pyramid has no mask series (see bin/extract_mask_series.py).
 */
process EXTRACT_MASK_SERIES {
    tag "${meta.patient_id}"
    label 'process_low'

    container "bolt3x/attend_image_analysis:merge"

    input:
    tuple val(meta), path(pyramid)

    output:
    tuple val(meta), path("cell_mask.tif")  , emit: cell_mask
    tuple val(meta), path("nuclei_mask.tif"), emit: nuclei_mask
    path "versions.yml"                     , emit: versions

    when:
    task.ext.when == null || task.ext.when

    script:
    """
    extract_mask_series.py --pyramid ${pyramid} --outdir .

    ${ProcessEnvelope.versions(task.process, ['tifffile', 'numpy'])}
    """

    stub:
    """
    touch cell_mask.tif nuclei_mask.tif

    ${ProcessEnvelope.versionsStub(task.process, ['tifffile', 'numpy'])}
    """
}
