/*
 * REG_MICRO_PREP - distributed MICRO-registration wave, stage 1
 *
 * Second non-rigid pass at micro resolution. Rebuilds rigid (IDENTICAL config to REG_PREP => same M),
 * INJECTS the wave-1 composed field onto each moving slide, then runs register_micro with the warper
 * no-op'd to CAPTURE the micro 2-D inputs cheaply (halt before micro DeepFlow, low RAM). Dumps, per
 * moving slide, micro tiler_inputs (the 2-D images REG_NONRIGID/REG_TILE read at micro resolution) +
 * micro_warp_state.json (micro full_out_shape_rc + mask_bbox for the additive pad in REG_COMPOSE_MICRO).
 *
 * Container: the EXTERNAL_TILE_HOOK-patched VALIS 1.0.0 image on Docker Hub
 * (bolt3x/attend_image_analysis:mirage_valis_1.0.0). Override via params.reg_dist_container.
 */
process REG_MICRO_PREP {
    tag "${patient_id}"
    label 'process_high'

    container "${params.reg_dist_container ?: 'bolt3x/attend_image_analysis:mirage_valis_1.0.0'}"

    input:
    tuple val(meta), val(patient_id), path(reference, stageAs: 'ref/*'), path(preproc_files, stageAs: 'input_?/*'), val(all_metas), path(prep_dir, stageAs: 'prep'), val(wave1_slides), path(wave1_bks, stageAs: 'w1_?/*')

    output:
    tuple val(patient_id), path("mprep"), val(all_metas), emit: prepped
    path "versions.yml"                                 , emit: versions
    path "*.size.csv"                                   , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    def ref_filename = reference ? reference.name : ''
    def tile_wh = params.reg_dist_micro_tile_wh ?: 2048
    def tile_buffer = params.reg_dist_tile_buffer ?: 100
    def memory_mode = params.memory_mode ?: 'high'
    def max_image_dim = params.reg_max_image_dim ?: 4000
    def micro_fraction = params.reg_micro_reg_fraction ?: 0.125
    // skip_micro must match REG_PREP's rigid stage; here micro is ON so MicroRigidRegistrar runs.
    def skip_micro = params.skip_micro_registration ? '--skip-micro-registration' : ''
    """
    total_bytes=\$(find -L ref input_* -maxdepth 1 -type f \\( -name "*.ome.tif" -o -name "*.ome.tiff" \\) -exec stat -L --printf="%s\\n" {} + 2>/dev/null | awk '{sum+=\$1} END {print sum}')
    echo "${task.process},${patient_id},inputs/,\${total_bytes:-0}" > ${patient_id}.REG_MICRO_PREP.size.csv

    mkdir -p staged mprep wave1
    find -L ref input_* -maxdepth 1 -type f \\( -name "*.ome.tif" -o -name "*.ome.tiff" \\) 2>/dev/null \\
        | xargs -P ${task.cpus} -I {} sh -c 'cp -Ln "{}" "staged/\$(basename "{}")" 2>/dev/null || true'

    # Reconstruct wave1/<slide>/bk.v from the per-slide REG_NONRIGID fields (staged w1_1/, w1_2/, ...
    # in the SAME order as the wave1_slides value list, both from the one groupTuple).
    slides="${wave1_slides.join(' ')}"
    i=1
    for s in \$slides; do
        mkdir -p "wave1/\$s"
        cp -L "w1_\$i/bk.v" "wave1/\$s/bk.v"
        i=\$((i+1))
    done

    reg_micro_prep.py \\
        --input-dir staged \\
        --out mprep \\
        --reference ${ref_filename} \\
        --prep-dir prep \\
        --wave1-dir wave1 \\
        --tile-wh ${tile_wh} \\
        --tile-buffer ${tile_buffer} \\
        --memory-mode ${memory_mode} \\
        --max-image-dim ${max_image_dim} \\
        --micro-fraction ${micro_fraction} \\
        ${skip_micro} \\
        ${args}

    find staged -maxdepth 1 -type f -delete

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
        valis: \$(python -c "import valis; print(valis.__version__)" 2>/dev/null || echo "unknown")
    END_VERSIONS
    """

    stub:
    def ref_filename = reference ? reference.name : 'ref.ome.tiff'
    """
    # Faithful stub: one micro dir per non-reference input slide stem (matches ch_micro_moving join keys).
    mkdir -p mprep
    ref_stem=\$(basename "${ref_filename}" | sed -E 's/\\.ome\\.tiff?\$//; s/\\.tiff?\$//')
    manifest='{"n_tiles":1,"n_rows":1,"n_cols":1,"tile_wh":2048,"tile_buffer":100,"has_mask":false,"has_target_stats":false,"processing_cls":null,"non_rigid_registrar_cls":"valis.non_rigid_registrars:OpticalFlowWarper"}'
    for f in \$(find -L ref input_* -maxdepth 1 -type f \\( -name "*.ome.tif" -o -name "*.ome.tiff" \\) 2>/dev/null); do
        stem=\$(basename "\$f" | sed -E 's/\\.ome\\.tiff?\$//; s/\\.tiff?\$//')
        [ "\$stem" = "\${ref_stem}" ] && continue
        [ -d "mprep/\${stem}/tiler_inputs" ] && continue
        mkdir -p "mprep/\${stem}/tiler_inputs"
        echo "\$manifest" > "mprep/\${stem}/tiler_inputs/manifest.json"
        python3 -c "import numpy as np; np.save('mprep/\${stem}/tiler_inputs/expanded_bboxes.npy', np.array([[0,0,10,10]]))" 2>/dev/null || touch "mprep/\${stem}/tiler_inputs/expanded_bboxes.npy"
        touch "mprep/\${stem}/tiler_inputs/moving.v" "mprep/\${stem}/tiler_inputs/fixed.v"
        echo '{"slide_name":"'\${stem}'","from_rigid_reg":false,"micro_full_out_shape_rc":[10,10],"micro_mask_bbox_xywh":[0,0,10,10]}' > "mprep/\${stem}/micro_warp_state.json"
    done
    echo "STUB,${patient_id},stub,0" > ${patient_id}.REG_MICRO_PREP.size.csv
    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        valis: stub
    END_VERSIONS
    """
}
