/*
 * MERGE_AND_PYRAMID - merge single-channel TIFFs into a pyramidal OME-TIFF for QuPath.
 *
 * Writes a pyramidal OME-TIFF directly (no bfconvert), preserving channel names, colors, and
 * pixel sizes in the OME-XML. Intensity channels only; cell objects are delivered separately via
 * cells.geojson. Processes large images memory-efficiently.
 */

process MERGE_AND_PYRAMID {
    tag "${meta.patient_id}"

    container "bolt3x/attend_image_analysis:merge"

    input:
    tuple val(meta), path(split_channels, stageAs: 'channels/*'), path(mask_files, stageAs: 'masks/*')

    output:
    tuple val(meta), path("pyramid.ome.tiff"), emit: pyramid
    path "versions.yml", emit: versions
    path("*.size.csv") , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''

    // Parameters are centralized in nextflow.config; no fallback needed here.
    def pixel_size_x = params.pixel_size
    def pixel_size_y = params.pixel_size
    def pyramid_resolutions = params.pyramid_resolutions
    def pyramid_scale = params.pyramid_scale
    def tile_size = params.tilex
    def compression = params.compression

    // Embed cell + nuclei segmentation masks as a second, single-resolution
    // uint32 OME series only when masks were actually staged.
    // subworkflows/local/assemble_export.nf is the single source of truth for the
    // embed_masks decision, for both the linear and the add_cycle path (it only
    // wires mask_files into this process's input when
    // embed_masks && quantify_compartments && expanded_quantification); an
    // empty mask_files list means Nextflow staged no masks/ dir.
    def masks_arg = mask_files ? "--masks-dir masks" : ""

    """
    # Log input size for tracing (channels/ dir only, -L follows symlinks)
    channels_bytes=\$(du -sLb channels/ | cut -f1)
    echo "${task.process},${meta.patient_id},channels/,\${channels_bytes}" > ${meta.patient_id}.MERGE_AND_PYRAMID.size.csv

    echo "Sample: ${meta.patient_id}"
    echo "Input directory: channels/"
    ls -lh channels/

    merge_channels_pyramid.py \\
        --input-dir channels \\
        --output pyramid.ome.tiff \\
        --physical-size-x ${pixel_size_x} \\
        --physical-size-y ${pixel_size_y} \\
        --pyramid-resolutions ${pyramid_resolutions} \\
        --pyramid-scale ${pyramid_scale} \\
        --tile-size ${tile_size} \\
        --compression ${compression} \\
        ${masks_arg} \\
        ${args}

    ${ProcessEnvelope.versions(task.process, ['tifffile', 'numpy'])}
    """

    stub:
    """
    touch pyramid.ome.tiff
    echo "STUB,${meta.patient_id},stub,0" > ${meta.patient_id}.MERGE_AND_PYRAMID.size.csv

    ${ProcessEnvelope.versionsStub(task.process, ['tifffile', 'numpy'])}
    """
}
