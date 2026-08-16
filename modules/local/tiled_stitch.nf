/*
 * TILED_STITCH - STARE fan-out step 4/4: warp the moving slide through the manifest.
 *
 * Applies M0 + mesh to every channel of the moving slide (bilinear, non-negative), in row strips
 * so peak memory is a strip. Writes the registered OME-TIFF in the reference frame.
 */
process TILED_STITCH {
    tag "${meta.patient_id}:${meta.channels.join('_')}"
    label 'process_medium'

    container 'bolt3x/attend_image_analysis:tiled'

    input:
    tuple val(meta), path(manifest), path(moving, stageAs: 'mov/*')

    output:
    tuple val(meta), path("registered/*_registered.ome.tiff"), emit: registered
    path "versions.yml"                                      , emit: versions
    path "*.size.csv"                                        , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def prefix    = "${meta.patient_id}_${meta.channels.join('_')}"
    def slidename = meta.channels.join('_')
    def out_tile  = params.reg_tiled_out_tile
    """
    total_bytes=\$(stat -L --printf="%s" ${moving} 2>/dev/null || echo 0)
    echo "${task.process},${meta.patient_id},${moving.name},\${total_bytes}" > ${prefix}.TILED_STITCH.size.csv

    mkdir -p registered
    tiled_stitch.py \\
        --moving ${moving} \\
        --manifest ${manifest} \\
        --moving-name '${slidename}' \\
        --out-tile ${out_tile} \\
        --pixel-size ${params.pixel_size} \\
        --channel-names ${meta.channels.join(' ')} \\
        --out registered/${prefix}_registered.ome.tiff

    ${ProcessEnvelope.versions(task.process, ['tifffile'])}
    """

    stub:
    def prefix = "${meta.patient_id}_${meta.channels.join('_')}"
    """
    mkdir -p registered
    touch registered/${prefix}_registered.ome.tiff
    echo "STUB,${meta.patient_id},stub,0" > ${prefix}.TILED_STITCH.size.csv
    ${ProcessEnvelope.versionsStub(task.process, ['tifffile'])}
    """
}
