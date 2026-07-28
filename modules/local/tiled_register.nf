/*
 * TILED_REGISTER - STARE tiled registration of one moving slide to the reference.
 *
 * Fully-parallel, JVM-free registration for laptop / low-end machines: a global rigid anchor
 * (ORB + RANSAC) refined by a TRE-gated per-tile mesh, warped bilinearly (non-negative). One
 * task per moving slide (patient-level and slide-level parallelism); memory is bounded because
 * the slide is processed tile-by-tile rather than held whole in a JVM heap.
 *
 * Input : reference + one moving OME-TIFF (+ the moving slide's meta)
 * Output: the registered OME-TIFF, a transform manifest (M0 + mesh, consumed by the reg_qc=2
 *         warper), and a per-slide TRE summary.
 */
process TILED_REGISTER {
    tag "${meta.patient_id}:${meta.channels.join('_')}"
    label 'process_medium'

    // Slim, JVM-free image (python + numpy + scipy + scikit-image + tifffile). No libvips/BioFormats.
    // TODO(release): build & pin this image; see containers/README.md.
    container 'bolt3x/attend_image_analysis:tiled'

    input:
    tuple val(meta), path(reference, stageAs: 'ref/*'), path(moving, stageAs: 'mov/*')

    output:
    tuple val(meta), path("registered/*_registered.ome.tiff"), emit: registered
    tuple val(meta), path("*_manifest.json")                 , emit: manifest
    tuple val(meta), path("*_tre.json")                      , emit: tre
    path "versions.yml"                                      , emit: versions
    path "*.size.csv"                                        , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args       = task.ext.args ?: ''
    def dapi_index = params.reg_tiled_dapi_index ?: 0
    def tile       = params.reg_tiled_tile ?: 2048
    def halo       = params.reg_tiled_halo ?: 256
    def gate       = params.reg_tiled_gate_tre ?: 1.0
    def slidename  = meta.channels.join('_')
    """
    total_bytes=\$(stat -L --printf="%s" ${moving} 2>/dev/null || echo 0)
    echo "${task.process},${meta.patient_id},${moving.name},\${total_bytes}" > ${meta.patient_id}_${slidename}.TILED_REGISTER.size.csv

    mkdir -p registered

    tiled_register.py \\
        --reference ${reference} \\
        --moving ${moving} \\
        --out registered/${meta.patient_id}_${slidename}_registered.ome.tiff \\
        --manifest ${meta.patient_id}_${slidename}_manifest.json \\
        --tre-summary ${meta.patient_id}_${slidename}_tre.json \\
        --dapi-index ${dapi_index} \\
        --tile ${tile} \\
        --halo ${halo} \\
        --gate-tre ${gate} \\
        --reference-name ${reference.simpleName} \\
        --moving-name ${slidename} \\
        ${args}

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
        scikit-image: \$(python -c "import skimage; print(skimage.__version__)" 2>/dev/null || echo "unknown")
    END_VERSIONS
    """

    stub:
    def slidename = meta.channels.join('_')
    """
    mkdir -p registered
    touch registered/${meta.patient_id}_${slidename}_registered.ome.tiff
    echo '{"ref_slide":"ref","slides":{"${slidename}":{"M0":[[1,0,0],[0,1,0],[0,0,1]],"mesh":null}}}' > ${meta.patient_id}_${slidename}_manifest.json
    echo '{"moving":"${slidename}","reference":"ref","n_tiles":0,"mesh_refined":false}' > ${meta.patient_id}_${slidename}_tre.json
    echo "STUB,${meta.patient_id},stub,0" > ${meta.patient_id}_${slidename}.TILED_REGISTER.size.csv

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        scikit-image: stub
    END_VERSIONS
    """
}
