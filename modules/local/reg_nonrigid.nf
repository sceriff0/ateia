/*
 * REG_NONRIGID - distributed registration, SEPARATED whole-image non-rigid (JVM-free)
 *
 * Runs OpticalFlowWarper whole-image on REG_PREP's 2-D inputs in a process with NO JVM. Bit-identical
 * to classic non-rigid (proven: OFW on the same 2-D inputs == classic moving_bk, max|d|=0) while
 * removing the BioFormats JVM heap from the non-rigid stage (the user's RAM hotspot). This is the
 * DEFAULT distributed non-rigid step; REG_TILE fan-out is the fallback for a single field too big
 * for one node (est_GB > threshold).
 */
process REG_NONRIGID {
    tag "${patient_id}:${slide}"
    label 'process_medium'

    container "${params.reg_dist_container ?: 'mirage-valis:1.0.0'}"

    input:
    tuple val(patient_id), val(slide), path(inputs_dir, stageAs: 'tiler_inputs')

    output:
    tuple val(patient_id), val(slide), path("nr/bk.v"), emit: field
    path "versions.yml"                               , emit: versions

    when:
    task.ext.when == null || task.ext.when

    script:
    """
    mkdir -p nr
    reg_nonrigid.py --inputs-dir tiler_inputs --out-dir nr

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        valis: \$(python -c "import valis; print(valis.__version__)" 2>/dev/null || echo "unknown")
    END_VERSIONS
    """

    stub:
    """
    mkdir -p nr
    touch nr/bk.v
    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        valis: stub
    END_VERSIONS
    """
}
