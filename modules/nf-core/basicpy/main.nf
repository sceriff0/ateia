process BASICPY {
    tag "$meta.id"
    label 'process_single'

    // DIGEST-PINNED (ruling R6). This is the vendored nf-core module's own
    // mcmicro-maintained image, docker.io/labsyspharm/basicpy-docker-mcmicro:1.2.0-patch5
    // as resolved 2026-09-02. Digest only, no tag — see modules/local/register.nf.
    container "docker.io/labsyspharm/basicpy-docker-mcmicro@sha256:355b14e2ec80b7b152272f333afd47234f007d0d37633b3ec948e87ec2c8e9b4"

    input:
    tuple val(meta), path(image)

    output:
    tuple val(meta), path("*-dfp.ome.tif"), path("*-ffp.ome.tif"), emit: profiles
    tuple val("${task.process}"), val('basicpy'), val("1.2.0"), emit: versions_basicpy, topic: versions
    // WARN: Version information not provided by tool on CLI. Please update this string when bumping
    when:
    task.ext.when == null || task.ext.when

    script:
    // Exit if running this module with -profile conda / -profile mamba
    if (workflow.profile.tokenize(',').intersect(['conda', 'mamba']).size() >= 1) {
        error "Basicpy module does not support Conda. Please use Docker / Singularity instead."
    }
    def args    = task.ext.args   ?: ''
    def prefix  = task.ext.prefix ?: "${meta.id}"
    """
    /opt/main.py -i $image -o . --output-flatfield $prefix --output-darkfield $prefix $args
    """

    stub:
    // Exit if running this module with -profile conda / -profile mamba
    if (workflow.profile.tokenize(',').intersect(['conda', 'mamba']).size() >= 1) {
        error "Basicpy module does not support Conda. Please use Docker / Singularity instead."
    }
    def prefix  = task.ext.prefix ?: "${meta.id}"
    """
    touch ${prefix}-dfp.ome.tif
    touch ${prefix}-ffp.ome.tif
    """
}
