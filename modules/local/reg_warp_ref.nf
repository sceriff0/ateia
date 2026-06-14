/*
 * REG_WARP_REF - distributed VALIS tiled registration: reference passthrough
 *
 * Warps the reference slide with its rigid M + crop only (identity non-rigid), via
 * reg_finalize.py --rigid-only, so downstream QC has the reference in the SAME cropped
 * coordinate space as the moving slides (classic VALIS warps every slide, incl. the reference).
 */
process REG_WARP_REF {
    tag "${patient_id}:${slide}"
    label 'process_medium'

    container "${params.reg_dist_container ?: 'mirage-valis:1.0.0'}"

    input:
    tuple val(patient_id), val(slide), path(warp_state, stageAs: 'warp_state.json'), path(src_slide, stageAs: 'src/*')

    output:
    tuple val(patient_id), val(slide), path("registered_slides/${slide}_registered.ome.tiff"), emit: registered
    path "versions.yml"                                                                       , emit: versions

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    """
    mkdir -p registered_slides
    reg_finalize.py \\
        --rigid-only \\
        --warp-state warp_state.json \\
        --src-slide ${src_slide} \\
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
    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        valis: stub
    END_VERSIONS
    """
}
