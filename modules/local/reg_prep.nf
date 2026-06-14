/*
 * REG_PREP - distributed VALIS tiled registration, stage 1 (spec §6.6)
 *
 * Runs VALIS rigid registration + produces base VALIS's processed 2-D non-rigid inputs, halting
 * BEFORE whole-image DeepFlow (low RAM). Dumps, per moving slide, the tiler-input contract
 * (2-D images + tile grid) and a plain warp_state.json — the Option-A handoff (no registrar pickle).
 *
 * Container MUST be the patched VALIS image (containers/valis/calc_hook.patch applied); the published
 * cdgatenbee/valis-wsi:1.0.0 lacks the EXTERNAL_TILE_HOOK seam. Set via params.reg_dist_container.
 */
process REG_PREP {
    // Fan-in over a patient's slides (like REGISTER): patient_id is the grouping key,
    // all_metas carries per-slide metadata.
    tag "${patient_id}"
    label 'process_high'

    container "${params.reg_dist_container ?: 'mirage-valis:1.0.0'}"

    input:
    tuple val(meta), val(patient_id), path(reference, stageAs: 'ref/*'), path(preproc_files, stageAs: 'input_?/*'), val(all_metas)

    output:
    tuple val(patient_id), path("prep"), val(all_metas), emit: prepped
    path "versions.yml"                                , emit: versions
    path "*.size.csv"                                  , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    def ref_filename = reference ? reference.name : ''
    def tile_wh = params.reg_dist_tile_wh ?: 512
    def tile_buffer = params.reg_dist_tile_buffer ?: 100
    // Mirror classic register.nf so rigid is bit-identical (memory_mode preset + micro-rigid).
    def memory_mode = params.memory_mode ?: 'high'
    def max_image_dim = params.reg_max_image_dim ?: 4000
    def skip_micro = params.skip_micro_registration ? '--skip-micro-registration' : ''
    """
    total_bytes=\$(find -L ref input_* -maxdepth 1 -type f \\( -name "*.ome.tif" -o -name "*.ome.tiff" \\) -exec stat -L --printf="%s\\n" {} + 2>/dev/null | awk '{sum+=\$1} END {print sum}')
    echo "${task.process},${patient_id},inputs/,\${total_bytes:-0}" > ${patient_id}.REG_PREP.size.csv

    # Copy inputs (deref symlinks) so VALIS keeps src_f (same reason as REGISTER).
    mkdir -p staged prep
    find -L ref input_* -maxdepth 1 -type f \\( -name "*.ome.tif" -o -name "*.ome.tiff" \\) 2>/dev/null \\
        | xargs -P ${task.cpus} -I {} sh -c 'cp -Ln "{}" "staged/\$(basename "{}")" 2>/dev/null || true'

    reg_prep.py \\
        --input-dir staged \\
        --out prep \\
        --reference ${ref_filename} \\
        --tile-wh ${tile_wh} \\
        --tile-buffer ${tile_buffer} \\
        --memory-mode ${memory_mode} \\
        --max-image-dim ${max_image_dim} \\
        ${skip_micro} \\
        ${args}

    # Drop the working copies; keep prep/ (the handoff).
    find staged -maxdepth 1 -type f -delete

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
        valis: \$(python -c "import valis; print(valis.__version__)" 2>/dev/null || echo "unknown")
    END_VERSIONS
    """

    stub:
    """
    mkdir -p prep/P001_mov1/tiler_inputs
    echo '{"n_tiles":1,"n_rows":1,"n_cols":1,"tile_wh":512,"tile_buffer":100,"has_mask":false,"has_target_stats":false,"processing_cls":null,"non_rigid_registrar_cls":"valis.non_rigid_registrars:OpticalFlowWarper"}' > prep/P001_mov1/tiler_inputs/manifest.json
    python3 -c "import numpy as np; np.save('prep/P001_mov1/tiler_inputs/expanded_bboxes.npy', np.array([[0,0,10,10]]))" 2>/dev/null || touch prep/P001_mov1/tiler_inputs/expanded_bboxes.npy
    touch prep/P001_mov1/tiler_inputs/moving.v prep/P001_mov1/tiler_inputs/fixed.v
    echo '{"slide_name":"P001_mov1","src_f":"P001_mov1.ome.tiff","from_rigid_reg":false,"M":[[1,0,0],[0,1,0],[0,0,1]],"processed_img_shape_rc":[10,10],"reg_img_shape_rc":[10,10],"aligned_slide_shape_rc":[10,10],"bbox_xywh":[0,0,10,10],"bg_color":[0,0,0],"series":0,"is_rgb":false,"interp_method":"bicubic","internal_pad":{"out_shape":[10,10],"bbox":[0,0,10,10]}}' > prep/P001_mov1/warp_state.json
    # Reference dir: warp_state only, NO tiler_inputs (the adapter routes it to REG_WARP_REF)
    mkdir -p prep/P001_ref
    echo '{"slide_name":"P001_ref","src_f":"P001_ref.ome.tiff","from_rigid_reg":false,"M":[[1,0,0],[0,1,0],[0,0,1]],"processed_img_shape_rc":[10,10],"reg_img_shape_rc":[10,10],"aligned_slide_shape_rc":[10,10],"bbox_xywh":[0,0,10,10],"bg_color":[0,0,0],"series":0,"is_rgb":false,"interp_method":"bicubic"}' > prep/P001_ref/warp_state.json
    echo "STUB,${patient_id},stub,0" > ${patient_id}.REG_PREP.size.csv
    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        valis: stub
    END_VERSIONS
    """
}
