/*
 * PREPROCESS - BaSiC illumination correction
 *
 * Applies BaSiC illumination correction with FOV tiling for flat-field and
 * dark-field estimation. Corrects shading artifacts in multi-channel images.
 *
 * BaSiC runs at its own defaults; the pipeline exposes only the FOV tile size
 * (params.preproc_tile_size) and whether the nuclear/fiducial channels are left
 * uncorrected (params.preproc_skip_nuclear). Whether this process runs at all is
 * params.skip_preprocessing, gated in subworkflows/local/preprocess.nf.
 *
 * Input: Raw OME-TIFF image with channel metadata
 * Output: Illumination-corrected OME-TIFF
 */
process PREPROCESS {
    tag "${meta.patient_id}"

    container "bolt3x/attend_image_analysis:preprocess"

    input:
    tuple val(meta), path(ome_tiff)

    output:
    tuple val(meta), path("*_corrected.ome.tif"), emit: preprocessed
    path "versions.yml"                         , emit: versions
    path("*.size.csv")                          , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    def skip_nuclear_flag = params.preproc_skip_nuclear ? '--skip_nuclear' : ''
    // The skip-the-fiducial decision is made in Python, so the marker list has to
    // travel with the call. MarkerUtils.markerList is the one sanctioned reader of
    // params.nuclear_markers -- it normalises the bare-String form Nextflow produces
    // for `--nuclear_markers CELLTOX` and the one-element comma-joined form a params
    // file can produce, both of which fail SILENTLY if read raw
    // (tests/test_nuclear_marker_routing.py). Same rule, same spelling, as
    // SPLIT_CHANNELS and CONVERT_IMAGE.
    def nuclear_args = "--nuclear-markers ${MarkerUtils.markerList(params.nuclear_markers).join(' ')}"
    def channels = meta.channels.join(' ')
    """
    # Log input size for tracing (-L follows symlinks)
    input_bytes=\$(stat -L --printf="%s" ${ome_tiff} 2>/dev/null || echo 0)
    echo "${task.process},${meta.patient_id},${ome_tiff.name},\${input_bytes}" > ${meta.patient_id}_${ome_tiff.simpleName}.PREPROCESS.size.csv

    preprocess.py \\
        --image ${ome_tiff} \\
        --output_dir . \\
        --channels ${channels} \\
        --fov_size ${params.preproc_tile_size} \\
        --pixel-size ${params.pixel_size} \\
        --n_workers ${task.cpus} \\
        ${skip_nuclear_flag} \\
        ${nuclear_args} \\
        ${args}

    ${ProcessEnvelope.versions(task.process, ['numpy'])}
    """

    stub:
    """
    touch ${ome_tiff.simpleName}_corrected.ome.tif
    touch ${ome_tiff.simpleName}_dims.txt
    echo "STUB,${meta.patient_id},stub,0" > ${meta.patient_id}_${ome_tiff.simpleName}.PREPROCESS.size.csv

    ${ProcessEnvelope.versionsStub(task.process, ['numpy'])}
    """
}