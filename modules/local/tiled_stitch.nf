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
    def strip     = params.reg_tiled_tile ?: 2048
    """
    total_bytes=\$(stat -L --printf="%s" ${moving} 2>/dev/null || echo 0)
    echo "${task.process},${meta.patient_id},${moving.name},\${total_bytes}" > ${prefix}.TILED_STITCH.size.csv

    mkdir -p registered
    tiled_stitch.py \\
        --moving ${moving} \\
        --manifest ${manifest} \\
        --moving-name '${slidename}' \\
        --strip ${strip} \\
        --out registered/${prefix}_registered.ome.tiff

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
        tifffile: \$(python -c "import tifffile; print(tifffile.__version__)" 2>/dev/null || echo "unknown")
    END_VERSIONS
    """

    stub:
    def prefix = "${meta.patient_id}_${meta.channels.join('_')}"
    """
    mkdir -p registered
    touch registered/${prefix}_registered.ome.tiff
    echo "STUB,${meta.patient_id},stub,0" > ${prefix}.TILED_STITCH.size.csv
    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        tifffile: stub
    END_VERSIONS
    """
}
