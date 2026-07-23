/*
 * REG_COMPOSE_FIELD - distributed registration, SEPARATED-mode finalize (fan-in per slide)
 *
 * Consumes the whole-image non-rigid field from REG_NONRIGID (--field) instead of stitched tiles,
 * then runs the SAME §6.3 compose as the tiled REG_COMPOSE_TILED. Used by the separated (JVM-free
 * whole-image) path; bit-identical to classic.
 */
process REG_COMPOSE_FIELD {
    tag "${patient_id}:${slide}"
    label 'process_high'

    container "${params.reg_dist_container ?: 'bolt3x/attend_image_analysis:mirage_valis_1.0.0'}"

    input:
    tuple val(patient_id), val(slide), path(inputs_dir, stageAs: 'tiler_inputs'), path(field, stageAs: 'nr/bk.v'), path(warp_state, stageAs: 'warp_state.json'), path(src_slide, stageAs: 'src/*')

    output:
    tuple val(patient_id), val(slide), path("slide_dxdy.v"), emit: field
    path "versions.yml"                                    , emit: versions
    path "*.size.csv"                                                                         , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    """
    in_bytes=\$(stat -L --printf="%s" ${src_slide} 2>/dev/null || echo 0)
    echo "${task.process},${patient_id},${slide},\${in_bytes:-0}" > ${patient_id}_${slide}.REG_COMPOSE_FIELD.size.csv

    reg_finalize.py \\
        --inputs-dir tiler_inputs \\
        --field nr/bk.v \\
        --warp-state warp_state.json \\
        --src-slide ${src_slide} \\
        --emit-field-only \\
        --out slide_dxdy.v \\
        ${args}

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
        valis: \$(python -c "import valis; print(valis.__version__)" 2>/dev/null || echo "unknown")
    END_VERSIONS
    """

    stub:
    """
    touch slide_dxdy.v
    echo "STUB,${patient_id},${slide},0" > ${patient_id}_${slide}.REG_COMPOSE_FIELD.size.csv
    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        valis: stub
    END_VERSIONS
    """
}
