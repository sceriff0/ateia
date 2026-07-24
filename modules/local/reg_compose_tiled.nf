/*
 * REG_COMPOSE_TILED - distributed VALIS tiled registration, stage 3 (fan-in per slide)
 *
 * Stitches the precomputed tile fields and reproduces VALIS's post-tiler composition, emitting the
 * composed full-res displacement field (slide_dxdy.v) — all from plain dumped data, no registrar
 * pickle; the compose is bit-identical to classic. The warp itself is deferred to the
 * REG_GRID -> REG_WARP_TILE -> REG_ASSEMBLE fan-out, which consumes this field.
 */
process REG_COMPOSE_TILED {
    tag "${patient_id}:${slide}"
    label 'process_high'

    container "${params.reg_dist_container ?: 'bolt3x/attend_image_analysis:mirage_valis_1.0.0'}"

    input:
    tuple val(patient_id), val(slide), path(inputs_dir, stageAs: 'tiler_inputs'), path(tiles, stageAs: 'tiles/*'), path(warp_state, stageAs: 'warp_state.json'), path(src_slide, stageAs: 'src/*')

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
    echo "${task.process},${patient_id},${slide},\${in_bytes:-0}" > ${patient_id}_${slide}.REG_COMPOSE_TILED.size.csv

    reg_finalize.py \\
        --inputs-dir tiler_inputs \\
        --tiles-dir tiles \\
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
    echo "STUB,${patient_id},${slide},0" > ${patient_id}_${slide}.REG_COMPOSE_TILED.size.csv
    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        valis: stub
    END_VERSIONS
    """
}
