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
    // Resolved the same way SEGMENT's CellSAM backend does (lib/SegBackends.groovy): from
    // THIS slide's channel metadata against params.nuclear_markers, with
    // params.reg_tiled_nuclear_index as an explicit override rather than the source of
    // truth. TILED_COARSE and TILED_REG_TILE must resolve the SAME index for a slide, so
    // the rule lives in MarkerUtils, not in fifteen duplicated lines per module.
    def nuclear_index = MarkerUtils.requireNuclearIndex(
        params.reg_tiled_nuclear_index,
        meta.channels,
        params.nuclear_markers,
        task.process,
        meta.patient_id)
    def upsample   = params.reg_tiled_upsample
    """
    tiled_reg_tile.py \\
        --reference ${reference} \\
        --moving ${moving} \\
        --m0 ${m0} \\
        --nuclear-index ${nuclear_index} \\
        ${TilePlan.regTileArgs(row)} \\
        --upsample ${upsample} \\
        --out ${prefix}_ctrl.json

    ${ProcessEnvelope.versions(task.process, [])}
    """

    stub:
    def prefix = "${meta.patient_id}_${meta.channels.join('_')}_${row.ix}_${row.iy}"
    """
    echo '{"ix":${row.ix},"iy":${row.iy},"cx":${row.cx},"cy":${row.cy},"dx":0,"dy":0,"tre":0}' > ${prefix}_ctrl.json
    ${ProcessEnvelope.versionsStub(task.process, [])}
    """
}
