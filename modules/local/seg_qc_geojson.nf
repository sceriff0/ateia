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
    label 'process_high'

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
    def args = task.ext.args ?: ''
    // Name the GeoJSON with VALIS's own slide-name convention (valtils.get_name strips
    // .ome.tif/.ome.tiff), so the stem == the registrar slide_dict key WARP_SEG_QC looks up.
    def prefix = image.name.replaceAll(/\.ome\.tiff?$/, '').replaceAll(/\.tiff?$/, '')
    def use_gpu_flag = params.seg_gpu ? '--use-gpu' : ''
    def pmin = params.seg_pmin
    def pmax = params.seg_pmax
    def n_tiles_y = params.seg_n_tiles_y
    def n_tiles_x = params.seg_n_tiles_x
    def model_dir_arg = params.segmentation_model_dir ? "--model-dir ${params.segmentation_model_dir}" : ''
    def prob_arg = (params.seg_prob_thresh != null) ? "--prob-thresh ${params.seg_prob_thresh}" : ''
    """
    bytes=\$(stat -L --printf="%s" ${image} 2>/dev/null || echo 0)
    echo "${task.process},${meta.patient_id},${image.name},\${bytes}" > ${prefix}.SEG_QC_GEOJSON.size.csv

    segment_to_geojson.py \\
        --image ${image} \\
        --output ${prefix}.geojson \\
        --model-name ${params.segmentation_model} \\
        ${model_dir_arg} \\
        ${use_gpu_flag} \\
        --pmin ${pmin} --pmax ${pmax} \\
        --n-tiles ${n_tiles_y} ${n_tiles_x} \\
        ${prob_arg} \\
        ${args}

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
        stardist: \$(python -c "import stardist; print(stardist.__version__)" 2>/dev/null || echo "unknown")
        scikit-image: \$(python -c "import skimage; print(skimage.__version__)" 2>/dev/null || echo "unknown")
    END_VERSIONS
    """

    stub:
    def prefix = image.name.replaceAll(/\.ome\.tiff?$/, '').replaceAll(/\.tiff?$/, '')
    """
    echo '{"type": "FeatureCollection", "features": []}' > ${prefix}.geojson
    echo "STUB,${meta.patient_id},stub,0" > ${prefix}.SEG_QC_GEOJSON.size.csv
    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        stardist: stub
        scikit-image: stub
    END_VERSIONS
    """
}
