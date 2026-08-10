/*
 * SEG_QC_GEOJSON - cell-mask label image -> cell GeoJSON (reg_qc = 2, GeoJSON path)
 *
 * Traces one Polygon per label out of SEG_QC_SEGMENT's `*_cell_mask.tif` and writes a
 * geometry-only cell GeoJSON. WARP_SEG_QC then warps those polygons through the
 * registrar and scores BEFORE-vs-AFTER overlap.
 *
 * THIS PROCESS NO LONGER SEGMENTS. It used to run StarDist itself, via
 * bin/segment_to_geojson.py, and was the pipeline's ONLY hardcoded segmenter: SEGMENT
 * dispatches on `params.seg_method` (lib/SegBackends.groovy) while this process always
 * ran StarDist with `starDistCommonFlags()`. Two consequences, both real:
 *
 *   1. At the shipped default (`seg_method = 'instantseg'`) the registration QC scored
 *      cells found by a DIFFERENT segmenter than the one whose masks the run ships.
 *   2. Worse, it could not run at all at stock defaults. `segmentation_model_dir` is
 *      null and `segmentation_model` is not a StarDist built-in, so
 *      segment_to_geojson.py raised FileNotFoundError -- and SEG_QC_GEOJSON sits in
 *      conf/modules.config's QC errorStrategy block, whose non-transient branch is
 *      'ignore'. reg_qc = 2 is the DEFAULT, so a stock run silently produced no
 *      registration QC and still exited 0. conf/test.config only escaped it by
 *      overriding segmentation_model to a real built-in ('2D_versatile_fluo').
 *
 * subworkflows/local/seg_qc.nf now segments the native slides with SEGMENT itself
 * (aliased SEG_QC_SEGMENT), so the QC follows `params.seg_method` by construction and
 * there is no second segmentation code path to keep in step. What is left here is the
 * label-image -> polygon half, which was already backend-agnostic: bin/mask_to_geojson.py
 * takes a plain label TIFF, and segment_to_geojson.py's last act was to call the very
 * same `mask_to_feature_collection()` on the very same in-memory cell mask. That is why
 * the polygons are unchanged for a given backend -- see
 * tests/test_seg_qc_geojson_equivalence.py, which pins the round trip.
 *
 * NAMING. The output stem must equal the registrar's slide_dict key, i.e. VALIS's own
 * slide-name convention (valtils.get_name strips .ome.tif/.ome.tiff). The mask's own
 * filename cannot supply it: the stardist backend names masks after the input image stem
 * (`foo.ome`), while instantseg/cellsam name them after `ext.prefix`. seg_qc.nf therefore
 * resolves the slide name ONCE, from the native image, and carries it as `meta.qc_slide`.
 */
process SEG_QC_GEOJSON {
    tag "${meta.patient_id}:${meta.qc_slide}"

    // The contour tracing is scipy.ndimage.find_objects + skimage find_contours /
    // approximate_polygon -- exactly what EXTRACT_CELL_PROPERTIES does, in the image
    // this repo already uses for that. Deliberately NOT a segmentation container: this
    // process must not depend on which backend produced the mask.
    container "bolt3x/attend_image_analysis:quantification_gpu"

    input:
    tuple val(meta), path(cell_mask)

    output:
    tuple val(meta), path("*.geojson"), emit: geojson
    path "versions.yml"               , emit: versions
    path("*.size.csv")                , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    // --tolerance comes from ext.args (conf/modules.config), carrying
    // params.simplify_tolerance -- the same Douglas-Peucker quantity
    // EXTRACT_CELL_PROPERTIES gets, which is what makes the two GeoJSONs comparable.
    def args   = task.ext.args ?: ''
    def prefix = meta.qc_slide
    """
    bytes=\$(stat -L --printf="%s" ${cell_mask} 2>/dev/null || echo 0)
    echo "${task.process},${meta.patient_id},${cell_mask.name},\${bytes}" > ${prefix}.SEG_QC_GEOJSON.size.csv

    mask_to_geojson.py \\
        --mask ${cell_mask} \\
        --out ${prefix}.geojson \\
        ${args}

    ${ProcessEnvelope.versions(task.process, ['skimage', 'scipy'])}
    """

    stub:
    def prefix = meta.qc_slide
    """
    echo '{"type": "FeatureCollection", "features": []}' > ${prefix}.geojson
    echo "STUB,${meta.patient_id},stub,0" > ${prefix}.SEG_QC_GEOJSON.size.csv
    ${ProcessEnvelope.versionsStub(task.process, ['skimage', 'scipy'])}
    """
}
