/*
 * EXPORT_SPATIALDATA - serialize postprocessing output as a SpatialData .zarr
 *
 * Writes the scverse-native representation of a patient: cell/nuclei label masks,
 * cell/nucleus polygons, and an AnnData table of per-compartment marker intensities
 * plus morphology — with registration and segmentation QC in `uns`, and per-cell
 * registration residuals in `obsm`.
 *
 * Nothing here is recomputed; every element is a re-serialization of an artifact
 * POSTPROCESSING already produced. The OME-TIFF and QuPath GeoJSON remain the
 * primary outputs — this is additive.
 *
 * The pyramid is written into the store only under `--spatialdata_include_image`.
 * spatialdata.write() materializes every element, so including a multi-cycle WSI
 * duplicates the largest artifact the pipeline makes; default off keeps stub and
 * CI runs cheap, and the flag produces a self-contained, depositable object.
 *
 * Optional inputs are passed as `[]` and simply stage nothing.
 */
process EXPORT_SPATIALDATA {
    tag "${meta.patient_id}"
    label params.spatialdata_include_image ? 'process_high_memory' : 'process_medium'

    container "bolt3x/attend_image_analysis:spatialdata"

    input:
    tuple val(meta), path(quant_csv), path(contours_json), path(nucleus_contours_json, stageAs: 'nucleus_contours.json'), path(cell_mask), path(nuclei_mask), path(pyramid)
    path qc_json
    path seg_eval_json
    path reg_residuals

    output:
    tuple val(meta), path("spatialdata/${meta.patient_id}.zarr"), emit: zarr
    path "versions.yml", emit: versions
    path("*.size.csv") , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    def nuc_mask_arg  = nuclei_mask ? "--nuclei-mask ${nuclei_mask}" : ''
    def nuc_cont_arg  = params.quantify_compartments ? "--nucleus-contours-json ${nucleus_contours_json}" : ''
    def image_arg     = (params.spatialdata_include_image && pyramid) ? "--include-image --pyramid ${pyramid}" : ''
    def qc_arg        = qc_json       ? "--qc-json ${qc_json}"             : ''
    def seg_arg       = seg_eval_json ? "--seg-eval-json ${seg_eval_json}" : ''
    def resid_arg     = reg_residuals ? "--reg-residuals ${reg_residuals}" : ''
    """
    input_bytes=\$(stat -L --printf="%s" ${quant_csv} 2>/dev/null || echo 0)
    echo "${task.process},${meta.patient_id},${quant_csv.name},\${input_bytes}" > ${meta.patient_id}.EXPORT_SPATIALDATA.size.csv

    mkdir -p spatialdata
    export_spatialdata.py \\
        --quant-csv ${quant_csv} \\
        --contours-json ${contours_json} \\
        --cell-mask ${cell_mask} \\
        ${nuc_mask_arg} \\
        ${nuc_cont_arg} \\
        ${image_arg} \\
        ${qc_arg} \\
        ${seg_arg} \\
        ${resid_arg} \\
        --patient-id ${meta.patient_id} \\
        --pixel-size ${params.pixel_size} \\
        --residual-join-max-px ${params.spatialdata_residual_join_max_px} \\
        -o spatialdata/${meta.patient_id}.zarr \\
        ${args}

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
        spatialdata: \$(python -c "import spatialdata; print(spatialdata.__version__)" 2>/dev/null || echo "unknown")
        anndata: \$(python -c "import anndata; print(anndata.__version__)" 2>/dev/null || echo "unknown")
        geopandas: \$(python -c "import geopandas; print(geopandas.__version__)" 2>/dev/null || echo "unknown")
        zarr: \$(python -c "import zarr; print(zarr.__version__)" 2>/dev/null || echo "unknown")
    END_VERSIONS
    """

    stub:
    """
    mkdir -p spatialdata/${meta.patient_id}.zarr
    echo '{"zarr_format":2}' > spatialdata/${meta.patient_id}.zarr/.zgroup
    echo "STUB,${meta.patient_id},stub,0" > ${meta.patient_id}.EXPORT_SPATIALDATA.size.csv

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        spatialdata: stub
        anndata: stub
        geopandas: stub
        zarr: stub
    END_VERSIONS
    """
}
