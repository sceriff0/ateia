/*
 * TILED_REG_TILE - STARE fan-out step 2/4: one tile's residual (the little-process fan-out).
 *
 * One task per tile: rigid-warps just this tile's reference-frame window of the moving DAPI and
 * phase-correlates it against the reference window. `row` is a tile-plan CSV row from TILED_COARSE.
 */
process TILED_REG_TILE {
    tag "${meta.patient_id}:${row.ix}_${row.iy}"
    label 'process_low'

    container 'bolt3x/attend_image_analysis:tiled'

    input:
    tuple val(meta), path(m0), path(reference, stageAs: 'ref/*'), path(moving, stageAs: 'mov/*'), val(row)

    output:
    tuple val(meta), path("*_ctrl.json"), emit: control
    path "versions.yml"                 , emit: versions

    when:
    task.ext.when == null || task.ext.when

    script:
    def prefix     = "${meta.patient_id}_${meta.channels.join('_')}_${row.ix}_${row.iy}"
    def dapi_index = params.reg_tiled_dapi_index
    def upsample   = params.reg_tiled_upsample
    """
    tiled_reg_tile.py \\
        --reference ${reference} \\
        --moving ${moving} \\
        --m0 ${m0} \\
        --dapi-index ${dapi_index} \\
        --ix ${row.ix} --iy ${row.iy} --cx ${row.cx} --cy ${row.cy} \\
        --rx0 ${row.rx0} --ry0 ${row.ry0} --rx1 ${row.rx1} --ry1 ${row.ry1} \\
        --upsample ${upsample} \\
        --out ${prefix}_ctrl.json

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
    END_VERSIONS
    """

    stub:
    def prefix = "${meta.patient_id}_${meta.channels.join('_')}_${row.ix}_${row.iy}"
    """
    echo '{"ix":${row.ix},"iy":${row.iy},"cx":${row.cx},"cy":${row.cy},"dx":0,"dy":0,"tre":0}' > ${prefix}_ctrl.json
    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
    END_VERSIONS
    """
}
