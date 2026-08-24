/*
 * ASHLAR_RETILE - ASHLAR arm step 1/3: stitched slide -> the uniform tile grid ashlar reads.
 *
 * ASHLAR needs raw, unstitched tiles with stage positions; mirage's registration inputs are
 * already-stitched WSIs. This synthesizes the tile input. Positions are never written: ashlar
 * COMPUTES them as [row, col] * tile_size * (1 - overlap), and bin/ashlar_retile.py lays the
 * grid on exactly that arithmetic, so they are exact by construction.
 *
 * Runs on EVERY slide including the reference, because ashlar's EdgeAligner stitches the
 * reference grid before LayerAligner can align anything onto it.
 */
process ASHLAR_RETILE {
    tag "${meta.patient_id}:${meta.channels.join('_')}"
    label 'process_low'

    // The retiler is tifffile/zarr/numpy only -- the ashlar image is needed for the SOLVE
    // step alone, which keeps the amd64-only pull off the per-slide fan-out.
    container 'bolt3x/mirage-tiled:1.0.0'

    input:
    tuple val(meta), path(image, stageAs: 'src/*')

    output:
    tuple val(meta), path("tiles"), emit: tiles
    path "versions.yml"           , emit: versions
    path "*.size.csv"             , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def prefix = "${meta.patient_id}_${meta.channels.join('_')}"
    """
    total_bytes=\$(stat -L --printf="%s" ${image} 2>/dev/null || echo 0)
    echo "${task.process},${meta.patient_id},${image.name},\${total_bytes}" > ${prefix}.ASHLAR_RETILE.size.csv

    ashlar_retile.py \\
        --image ${image} \\
        --outdir tiles \\
        --tile-size ${params.reg_ashlar_tile} \\
        --overlap ${params.reg_ashlar_overlap}

    ${ProcessEnvelope.versions(task.process, ['tifffile'])}
    """

    stub:
    def prefix = "${meta.patient_id}_${meta.channels.join('_')}"
    """
    mkdir -p tiles
    touch tiles/r000_c000.tif
    echo '{"cycle":0,"pattern":"r{row:03}_c{col:03}.tif","n_rows":1,"n_cols":1,"tile_size":16,"overlap":0.1,"stride":14,"pixel_size_um":0.325,"n_channels":1,"orig_shape":[16,16],"source":"stub","valid_extent":[[0,0,16,16]]}' > tiles/grid.json
    echo "STUB,${meta.patient_id},stub,0" > ${prefix}.ASHLAR_RETILE.size.csv
    ${ProcessEnvelope.versionsStub(task.process, ['tifffile'])}
    """
}
