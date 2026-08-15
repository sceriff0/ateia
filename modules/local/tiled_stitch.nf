/*
 * TILED_STITCH - STARE fan-out step 4/4: warp the moving slide through the manifest.
 *
 * Applies M0 + mesh to every channel of the moving slide (bilinear, non-negative), in row strips
 * so peak memory is a strip. Writes the registered OME-TIFF in the reference frame.
 *
 * The output subdirectory is `registered_slides/`, mirroring lib/Layout.groovy's
 * REGISTERED_SUBDIR -- publishDir carries a producer's subdirectory into the published path,
 * so this name IS half of where a tiled run's slides end up. It used to be `registered/`,
 * which published the same logical slide to <pid>/registered/registered/ while VALIS's went
 * to <pid>/registered/registered_slides/. tests/test_layout.py checks the three producers
 * and conf/modules.config against the constant.
 */
process TILED_STITCH {
    tag "${meta.patient_id}:${meta.channels.join('_')}"
    label 'process_medium'

    container 'bolt3x/attend_image_analysis:tiled'

    input:
    tuple val(meta), path(manifest), path(moving, stageAs: 'mov/*')

    output:
    tuple val(meta), path("registered_slides/*_registered.ome.tiff"), emit: registered
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

    mkdir -p registered_slides
    tiled_stitch.py \\
        --moving ${moving} \\
        --manifest ${manifest} \\
        --moving-name '${slidename}' \\
        --out-tile ${out_tile} \\
        --pixel-size ${params.pixel_size} \\
        --out registered_slides/${prefix}_registered.ome.tiff

    ${ProcessEnvelope.versions(task.process, ['tifffile'])}
    """

    stub:
    def prefix = "${meta.patient_id}_${meta.channels.join('_')}"
    """
    mkdir -p registered_slides
    touch registered_slides/${prefix}_registered.ome.tiff
    echo "STUB,${meta.patient_id},stub,0" > ${prefix}.TILED_STITCH.size.csv
    ${ProcessEnvelope.versionsStub(task.process, ['tifffile'])}
    """
}
