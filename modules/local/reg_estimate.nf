/*
 * REG_ESTIMATE - distributed tiling router (spec §6.5/§8.1)
 *
 * Computes VALIS's own non-rigid tiling decision (est_GB > TILER_THRESH_GB) per patient from slide
 * metadata (no JVM, no registration), so the subworkflow can route:
 *   est_GB > threshold  -> distributed tiling (== classic-tiled regime)
 *   est_GB <= threshold -> classic whole-image REGISTER (identical to classic)
 * This is what makes <10GB inputs bit-identical to classic for every modality.
 */
process REG_ESTIMATE {
    tag "${patient_id}"
    label 'process_single'

    container "${params.reg_dist_container ?: 'mirage-valis:1.0.0'}"

    input:
    tuple val(patient_id), path(reference, stageAs: 'ref/*'), path(all_files, stageAs: 'imgs/*')

    output:
    tuple val(patient_id), env(USE_TILER), emit: decision
    path "est_gb.json"                   , emit: report

    when:
    task.ext.when == null || task.ext.when

    script:
    def max_nr = params.reg_max_non_rigid_dim ?: 4096
    def thr = params.reg_dist_threshold_gb ?: 10
    """
    estimate_reg_gb.py \\
        --reference ${reference} \\
        --images \$(find -L imgs -maxdepth 1 -type f \\( -name '*.ome.tif' -o -name '*.ome.tiff' \\) | sort | tr '\\n' ' ') \\
        --max-non-rigid-dim ${max_nr} \\
        --threshold-gb ${thr} \\
        --out est_gb.json
    USE_TILER=\$(python3 -c "import json; print('true' if json.load(open('est_gb.json'))['use_tiler'] else 'false')")
    """

    stub:
    """
    echo '{"est_gb":0.0,"use_tiler":false,"threshold_gb":10}' > est_gb.json
    USE_TILER=false
    """
}
