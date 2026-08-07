/*
 * SEG_QC_GEOJSON - native-slide segmentation -> cell GeoJSON (reg_qc = 2, GeoJSON path)
 *
 * Segments a single slide's DAPI on its NATIVE (pre-registration) image with StarDist and
 * writes a geometry-only cell GeoJSON. WARP_SEG_QC then warps these polygons through the
 * registrar. Segmenting the clean native image (not a warped one) isolates registration
 * quality from segment-on-interpolated-pixels bias. Same StarDist container + params as SEGMENT.
 */
process SEG_QC_GEOJSON {
    tag "${meta.patient_id}:${image.simpleName}"

    container "bolt3x/attend_image_analysis:segmentation_gpu"

    input:
    tuple val(meta), path(image)

    output:
    tuple val(meta), path("*.geojson"), emit: geojson
    path "versions.yml"               , emit: versions
    path("*.size.csv")                , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    // The StarDist flags (model, tiling, thresholds, --tolerance) come from ext.args now
    // (conf/modules.config), shared with SEGMENT via that config file's
    // starDistCommonFlags() -- see the withName: 'SEG_QC_GEOJSON' block's comment for
    // what each flag is.
    def args = task.ext.args ?: ''
    // Name the GeoJSON with VALIS's own slide-name convention (valtils.get_name strips
    // .ome.tif/.ome.tiff), so the stem == the registrar slide_dict key WARP_SEG_QC looks up.
    def prefix = image.name.replaceAll(/\.ome\.tiff?$/, '').replaceAll(/\.tiff?$/, '')
    // --nuclear-markers stays built HERE rather than in ext.args, unlike the rest of the
    // StarDist flags: lib/MarkerUtils.groovy is on the classpath the pipeline SCRIPT
    // compiles with, but conf/modules.config is parsed by a separate Nextflow
    // ConfigParser pass that cannot resolve it (confirmed the same way as
    // starDistCommonFlags() -- see that function's comment).
    //
    // The nuclear channel is resolved inside the Python script too (no --dapi-channel is
    // passed), so the configured marker list has to travel with it. Without this the
    // script fell back to bin/utils/metadata.py's DEFAULT_NUCLEAR_MARKERS, and a run
    // configured for a marker outside that default scored registration QC on the wrong
    // channel -- silently, because find_nuclear_index() falls back to index 0 rather
    // than failing.
    def nuclear_args = "--nuclear-markers ${MarkerUtils.markerList(params.nuclear_markers).join(' ')}"
    """
    bytes=\$(stat -L --printf="%s" ${image} 2>/dev/null || echo 0)
    echo "${task.process},${meta.patient_id},${image.name},\${bytes}" > ${prefix}.SEG_QC_GEOJSON.size.csv

    segment_to_geojson.py \\
        --image ${image} \\
        --output ${prefix}.geojson \\
        ${nuclear_args} \\
        ${args}

    ${ProcessEnvelope.versions(task.process, ['stardist', 'skimage'])}
    """

    stub:
    def prefix = image.name.replaceAll(/\.ome\.tiff?$/, '').replaceAll(/\.tiff?$/, '')
    """
    echo '{"type": "FeatureCollection", "features": []}' > ${prefix}.geojson
    echo "STUB,${meta.patient_id},stub,0" > ${prefix}.SEG_QC_GEOJSON.size.csv
    ${ProcessEnvelope.versionsStub(task.process, ['stardist', 'skimage'])}
    """
}
