/*
 * REG_WARP_TILE - warp ONE output tile of one slide's full-res warp.
 *
 * This is the fan-out that removes the RAM wall: N of these run independently instead of one
 * process holding a full-resolution slide. Each tile is a .crop() of the SAME lazy pyvips warp,
 * and pyvips is demand-driven, so cropping the OUTPUT pulls exactly the source pixels that region
 * needs -- including the interpolator's halo across the tile edge -- through VALIS's own
 * warp_tools.warp_img. The reassembled slide is therefore bit-identical to a single-process warp
 * BY CONSTRUCTION, not by tolerance (verified by the integration tests).
 *
 * Peak RAM is O(one output tile + its source footprint), so `process_low` is deliberate.
 */
process REG_WARP_TILE {
    tag "${patient_id}:${slide}:${tile_idx}"
    label 'process_low'

    container "${params.reg_dist_container ?: 'bolt3x/attend_image_analysis:mirage_valis_1.0.0'}"

    input:
    tuple val(patient_id), val(slide), path(warp_state, stageAs: 'warp_state.json'), path(src_slide, stageAs: 'src/*'), path(field), path(grid, stageAs: 'grid.json'), val(tile_idx)

    output:
    tuple val(patient_id), val(slide), path("tiles/tile_*"), emit: tile
    path "versions.yml"                                    , emit: versions

    when:
    task.ext.when == null || task.ext.when

    script:
    // Must match REG_GRID's choice for this slide: the grid was computed from the same warp.
    def field_arg = field ? "--field ${field}" : "--rigid-only"
    def args = task.ext.args ?: ''
    """
    reg_warp_tile.py \\
        --warp-state warp_state.json \\
        --src-slide ${src_slide} \\
        ${field_arg} \\
        --grid grid.json \\
        --tile-idx ${tile_idx} \\
        --out-dir tiles \\
        --tile-format ${params.reg_warp_tile_format} \\
        --tile-compression ${params.reg_warp_tile_compression} \\
        ${args}

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
        valis: \$(python -c "import valis; print(valis.__version__)" 2>/dev/null || echo "unknown")
    END_VERSIONS
    """

    stub:
    """
    mkdir -p tiles
    touch tiles/tile_${tile_idx}.tif

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        valis: stub
    END_VERSIONS
    """
}
