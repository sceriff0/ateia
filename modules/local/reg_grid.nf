/*
 * REG_GRID - compute the output-tile grid for one slide's full-res warp.
 *
 * The canvas size is read from the ACTUAL lazily-warped pyvips image rather than derived from
 * warp_state arithmetic, so the grid can never disagree with what REG_WARP_TILE produces (that
 * process re-checks the match and fails loudly if it ever does). Cheap: pyvips knows an
 * unevaluated image's dimensions, so nothing is decoded here.
 *
 * `path(field)` carries no `arity`, so the adapter can pass `[]` for the reference slide and it
 * renders as an empty string -- that is how --rigid-only is selected. (`arity: '0..1'` combined
 * with `stageAs` is rejected by Nextflow.)
 */
process REG_GRID {
    tag "${patient_id}:${slide}"
    label 'process_low'

    container "${params.reg_dist_container ?: 'bolt3x/attend_image_analysis:mirage_valis_1.0.0'}"

    input:
    tuple val(patient_id), val(slide), path(warp_state, stageAs: 'warp_state.json'), path(src_slide, stageAs: 'src/*'), path(field)

    output:
    tuple val(patient_id), val(slide), path("grid.json"), emit: grid
    path "versions.yml"                                 , emit: versions

    when:
    task.ext.when == null || task.ext.when

    script:
    // field == [] -> --rigid-only, matching reg_finalize.py --rigid-only exactly (dxdy=None).
    // Do NOT substitute a zero field: warp_img branches differently on None vs a supplied field.
    def field_arg = field ? "--field ${field}" : "--rigid-only"
    def args = task.ext.args ?: ''
    """
    reg_assemble.py \\
        --warp-state warp_state.json \\
        --src-slide ${src_slide} \\
        ${field_arg} \\
        --tile-wh ${params.reg_warp_tile_wh} \\
        --write-grid grid.json \\
        ${args}

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
        valis: \$(python -c "import valis; print(valis.__version__)" 2>/dev/null || echo "unknown")
    END_VERSIONS
    """

    stub:
    """
    echo '{"n_cols":1,"n_rows":1,"tile_wh":4096,"width":8,"height":8,"tiles":[{"idx":0,"x":0,"y":0,"w":8,"h":8}]}' > grid.json

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        valis: stub
    END_VERSIONS
    """
}
