/*
 * REG_ASSEMBLE - join the warped output tiles into the final registered OME-TIFF.
 *
 * Streaming: the joined image stays unevaluated until tiffsave pulls it, and libvips evaluates the
 * join region-by-region as it writes, so peak RAM is O(one row of tiles) rather than O(slide).
 * Tiles are joined edge-to-edge with NO blending, which is correct because each is a crop of the
 * same demand-driven warp -- see modules/local/reg_warp_tile.nf.
 *
 * reg_assemble.py validates that every tile exists and is the exact region grid.json claims, and
 * that the rows form a regular lattice; pyvips' join defaults to expand=false, so an unchecked
 * short row would silently CROP the slide instead of failing.
 */
process REG_ASSEMBLE {
    tag "${patient_id}:${slide}"
    label 'process_medium'

    container "${params.reg_dist_container ?: 'bolt3x/attend_image_analysis:mirage_valis_1.0.0'}"

    input:
    tuple val(patient_id), val(slide), path(warp_state, stageAs: 'warp_state.json'), path(src_slide, stageAs: 'src/*'), path(grid, stageAs: 'grid.json'), path(tiles, stageAs: "tiles/*")

    output:
    tuple val(patient_id), val(slide), path("registered_slides/${slide}_registered.ome.tiff"), emit: registered
    path "versions.yml"                                                                      , emit: versions
    path "*.size.csv"                                                                        , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    """
    in_bytes=\$(stat -L --printf="%s" ${src_slide} 2>/dev/null || echo 0)
    echo "${task.process},${patient_id},${slide},\${in_bytes:-0}" > ${patient_id}_${slide}.REG_ASSEMBLE.size.csv

    mkdir -p registered_slides
    reg_assemble.py \\
        --warp-state warp_state.json \\
        --src-slide ${src_slide} \\
        --grid grid.json \\
        --tiles-dir tiles \\
        --out registered_slides/${slide}_registered.ome.tiff \\
        ${args}

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
        valis: \$(python -c "import valis; print(valis.__version__)" 2>/dev/null || echo "unknown")
    END_VERSIONS
    """

    stub:
    """
    mkdir -p registered_slides
    touch registered_slides/${slide}_registered.ome.tiff
    echo "STUB,${patient_id},${slide},0" > ${patient_id}_${slide}.REG_ASSEMBLE.size.csv

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        valis: stub
    END_VERSIONS
    """
}
