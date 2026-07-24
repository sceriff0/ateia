/*
 * COMPARE_REGISTRATION - diff the classic and low-memory registered slides for one image.
 *
 * Driven by --reg_compare. Both inputs are staged into separate subdirectories because the two
 * paths produce files with the SAME name (<slide>_registered.ome.tiff); without stageAs one would
 * silently clobber the other and the script would compare a file with itself.
 *
 * Streams both slides tile-by-tile, so this runs in bounded RAM on the same low-resource machine
 * the new path targets rather than needing 2x a slide in memory to check a memory optimisation.
 */
process COMPARE_REGISTRATION {
    tag "${meta.id ?: meta.patient_id}"
    label 'process_low'

    container "${params.reg_dist_container ?: 'bolt3x/attend_image_analysis:mirage_valis_1.0.0'}"

    input:
    tuple val(meta), path(classic, stageAs: 'classic/*'), path(candidate, stageAs: 'candidate/*')

    output:
    tuple val(meta), path("*_regcompare.json"), emit: metrics
    tuple val(meta), path("*_regdiff.png"),     emit: diff_png
    path "versions.yml",                        emit: versions

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    def name = "${meta.patient_id}_${(meta.channels ?: []).join('-') ?: (meta.id ?: 'slide')}"
    """
    compare_registration.py \\
        --a ${classic} \\
        --b ${candidate} \\
        --slide ${name} \\
        --out-json ${name}_regcompare.json \\
        --out-png ${name}_regdiff.png \\
        ${args}

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python3 --version | sed 's/Python //')
        pyvips: \$(python3 -c "import pyvips; print(pyvips.__version__)")
    END_VERSIONS
    """

    stub:
    def name = "${meta.patient_id}_${(meta.channels ?: []).join('-') ?: (meta.id ?: 'slide')}"
    """
    echo '{"slide":"${name}","overall":{"max_abs":0.0,"mean_abs":0.0,"rmse":0.0,"pct_differing":0.0}}' > ${name}_regcompare.json
    touch ${name}_regdiff.png

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        pyvips: stub
    END_VERSIONS
    """
}
