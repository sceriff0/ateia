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
    // Resolve the nuclear/fiducial channel the transform is estimated from, the same
    // way SEGMENT's CellSAM backend does (lib/SegBackends.groovy): from THIS slide's
    // channel metadata against params.nuclear_markers. params.reg_tiled_nuclear_index
    // is an explicit override, not the source of truth -- the old fixed param restated
    // an invariant CONVERT_IMAGE already guarantees, and named it after one marker, so
    // a CELLTOX panel was read through something called "the DAPI index".
    def nuclear_index = params.reg_tiled_nuclear_index != null
        ? params.reg_tiled_nuclear_index
        : MarkerUtils.nuclearIndex(meta.channels ?: [], params.nuclear_markers)
    if (nuclear_index < 0)
        throw new IllegalArgumentException(
            "${task.process}: no nuclear/fiducial channel among ${meta.channels} for " +
            "patient ${meta.patient_id}. Configured nuclear_markers: " +
            "${MarkerUtils.markerList(params.nuclear_markers).join(', ')}. " +
            "Set params.reg_tiled_nuclear_index to override.")
    def upsample   = params.reg_tiled_upsample
    """
    tiled_reg_tile.py \\
        --reference ${reference} \\
        --moving ${moving} \\
        --m0 ${m0} \\
        --nuclear-index ${nuclear_index} \\
        --ix ${row.ix} --iy ${row.iy} --cx ${row.cx} --cy ${row.cy} \\
        --rx0 ${row.rx0} --ry0 ${row.ry0} --rx1 ${row.rx1} --ry1 ${row.ry1} \\
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
