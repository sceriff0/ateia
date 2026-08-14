/*
 * CONVERT_IMAGE - convert an input slide to standardized OME-TIFF via Bio-Formats.
 */
process CONVERT_IMAGE {
    tag "${meta.patient_id}"

    container "bolt3x/attend_image_analysis:convert_bioformats_2"

    input:
    tuple val(meta), path(image_file)

    output:
    tuple val(meta), path("*.ome.tif"), path("*_channels.txt"), emit: ome_tiff
    path "versions.yml"                                       , emit: versions
    path("*.size.csv")                                        , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    def prefix = task.ext.prefix ?: (meta.id ?: meta.patient_id)
    def pixel_size = params.pixel_size
    def channels = meta.channels.join(',')
    // Through MarkerUtils, like every other consumer. params.nuclear_markers is a List
    // in nextflow.config but a bare String for `--nuclear_markers CELLTOX` on the command
    // line, and "CELLTOX".join(' ') does NOT throw -- it dispatches to Java's static
    // String.join(CharSequence, CharSequence...) with zero varargs and returns the EMPTY
    // string, so the rendered command carries a bare `--nuclear-markers` and fails
    // convert_image.py's nargs="+" with exit 2 halfway through the run.
    def nuclear_markers = MarkerUtils.markerList(params.nuclear_markers).join(' ')
    """
    # Log input size for tracing (-L follows symlinks)
    input_bytes=\$(stat -L --printf="%s" ${image_file} 2>/dev/null || echo 0)
    echo "${task.process},${meta.patient_id},${image_file.name},\${input_bytes}" > ${meta.patient_id}_${image_file.simpleName}.CONVERT_IMAGE.size.csv

    convert_image.py \\
        --input_file ${image_file} \\
        --output_dir . \\
        --patient_id ${prefix} \\
        --channels ${channels} \\
        --pixel-size ${pixel_size} \\
        --nuclear-markers ${nuclear_markers} \\
        ${args}

    ${ProcessEnvelope.versions(task.process, ['tifffile', 'aicsimageio', 'h5py'])}
    """

    stub:
    def prefix = task.ext.prefix ?: (meta.id ?: meta.patient_id)
    def channels = meta.channels.join(',')
    """
    touch ${prefix}.ome.tif
    echo "${channels}" > ${prefix}_channels.txt
    echo "STUB,${meta.patient_id},stub,0" > ${meta.patient_id}_${image_file.simpleName}.CONVERT_IMAGE.size.csv

    ${ProcessEnvelope.versionsStub(task.process, ['tifffile', 'aicsimageio', 'h5py'])}
    """
}
