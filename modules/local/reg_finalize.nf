/*
 * REG_FINALIZE - distributed VALIS tiled registration, stage 3 (fan-in per slide)
 *
 * Stitches the precomputed tile fields, reproduces VALIS's post-tiler composition, and warps+saves
 * the full-res slide via VALIS's own streaming slide_tools.warp_slide — all from plain dumped data,
 * no registrar pickle (spec §6.3/§6.6, compose+warp proven bit-identical by Task 4.5).
 */
process REG_FINALIZE {
    tag "${patient_id}:${slide}"
    label 'process_high'

    container "${params.reg_dist_container ?: 'bolt3x/attend_image_analysis:mirage_valis_1.0.0'}"

    input:
    tuple val(patient_id), val(slide), path(inputs_dir, stageAs: 'tiler_inputs'), path(tiles, stageAs: 'tiles/*'), path(warp_state, stageAs: 'warp_state.json'), path(src_slide, stageAs: 'src/*')

    output:
    tuple val(patient_id), val(slide), path("registered_slides/${slide}_registered.ome.tiff"), emit: registered
    path "versions.yml"                                                                       , emit: versions
    path "*.size.csv"                                                                         , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    """
    in_bytes=\$(stat -L --printf="%s" ${src_slide} 2>/dev/null || echo 0)
    echo "${task.process},${patient_id},${slide},\${in_bytes:-0}" > ${patient_id}_${slide}.REG_FINALIZE.size.csv

    mkdir -p registered_slides
    reg_finalize.py \\
        --inputs-dir tiler_inputs \\
        --tiles-dir tiles \\
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
    echo "STUB,${patient_id},${slide},0" > ${patient_id}_${slide}.REG_FINALIZE.size.csv
    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        valis: stub
    END_VERSIONS
    """
}
