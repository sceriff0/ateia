/*
 * REG_FINALIZE_MICRO - distributed registration, SEPARATED-mode finalize WITH micro second wave
 *
 * Same as REG_FINALIZE_FIELD but additively composes the micro residual (REG_NONRIGID on the micro
 * 2-D inputs from REG_MICRO_PREP) onto the wave-1 field before warping — reproducing classic
 * register_micro (registration.py:4299-4330; additive compose proven max|Δ|=0 by spike_micro_option2).
 */
process REG_FINALIZE_MICRO {
    tag "${patient_id}:${slide}"
    label 'process_high'

    container "${params.reg_dist_container ?: 'bolt3x/attend_image_analysis:mirage_valis_1.0.0'}"

    input:
    tuple val(patient_id), val(slide), path(inputs_dir, stageAs: 'tiler_inputs'), path(field, stageAs: 'nr/bk.v'), path(warp_state, stageAs: 'warp_state.json'), path(src_slide, stageAs: 'src/*'), path(micro_inputs, stageAs: 'micro_inputs'), path(micro_field, stageAs: 'micro_nr/bk.v'), path(micro_warp_state, stageAs: 'micro_warp_state.json')

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
    echo "${task.process},${patient_id},${slide},\${in_bytes:-0}" > ${patient_id}_${slide}.REG_FINALIZE_MICRO.size.csv

    mkdir -p registered_slides
    reg_finalize.py \\
        --inputs-dir tiler_inputs \\
        --field nr/bk.v \\
        --warp-state warp_state.json \\
        --src-slide ${src_slide} \\
        --micro-field micro_nr/bk.v \\
        --micro-warp-state micro_warp_state.json \\
        --micro-inputs-dir micro_inputs \\
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
    echo "STUB,${patient_id},${slide},0" > ${patient_id}_${slide}.REG_FINALIZE_MICRO.size.csv
    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        valis: stub
    END_VERSIONS
    """
}
