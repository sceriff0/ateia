process PUBLISH_REFERENCE {
    tag "${meta.id ?: meta.patient_id}"
    label 'process_single'
    container null  // Pass-through process: no computation, runs on executor node

    input:
    tuple val(meta), path(image)

    output:
    tuple val(meta), path(image), emit: published
    path "versions.yml"          , emit: versions

    when:
    task.ext.when == null || task.ext.when

    script:
    """
    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        bash: \$(bash --version | head -n1 | sed 's/GNU bash, version //')
    END_VERSIONS
    """

    stub:
    """
    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        bash: stub
    END_VERSIONS
    """
}
