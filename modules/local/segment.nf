/*
 * SEGMENT - cell segmentation
 *
 * Dispatches between three backends, controlled by ``params.seg_method``:
 *   - 'stardist'   (default): StarDist on the DAPI channel (channel 0 hard-check).
 *   - 'instantseg':           InstanSeg with the ``fluorescence_nuclei_and_cells``
 *                             model. Channel-invariant: consumes the multichannel
 *                             image directly; no DAPI-channel-0 check is enforced.
 *   - 'cellsam':              CellSAM (SAM foundation model) on the DAPI channel.
 *                             Segments nuclei; the whole-cell mask is derived by
 *                             expanding nuclei labels (StarDist-style), so a
 *                             nuclear channel is required but no channel-0 hard
 *                             check is enforced (passed explicitly as --dapi-channel).
 *
 * All backends produce the same outputs (``*_nuclei_mask.tif`` /
 * ``*_cell_mask.tif`` / ``versions.yml`` / ``*.size.csv``) so all downstream
 * modules are contract-preserving.
 *
 * Container is selected dynamically from ``params.seg_method``.
 */
process SEGMENT {
    tag "${meta.patient_id}"
    label 'process_high'

    container { params.seg_method == 'cellsam'
                ? 'bolt3x/attend_image_analysis:cellsam'
                : params.seg_method == 'instantseg'
                ? 'bolt3x/attend_image_analysis:instant_seg'
                : 'bolt3x/attend_image_analysis:segmentation_gpu' }

    input:
    tuple val(meta), path(merged_file)

    output:
    tuple val(meta), path("*_nuclei_mask.tif"), emit: nuclei_mask
    tuple val(meta), path("*_cell_mask.tif")  , emit: cell_mask
    path "versions.yml"                        , emit: versions
    path("*.size.csv")                         , emit: size_log

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    def prefix = task.ext.prefix ?: "${meta.patient_id}"
    def use_gpu_flag = params.seg_gpu ? '--use-gpu' : ''
    def pmin = params.seg_pmin ?: 1.0
    def pmax = params.seg_pmax ?: 99.8
    def n_tiles_y = params.seg_n_tiles_y ?: 1
    def n_tiles_x = params.seg_n_tiles_x ?: 1
    def dapi_check = (meta.channels && meta.channels.size() > 0) ? meta.channels[0].toUpperCase() : 'UNKNOWN'
    // Pass --model-dir only when a custom StarDist model directory is configured.
    // When unset, segment.py loads --model-name as a built-in pretrained model
    // (e.g. 2D_versatile_fluo), so the pipeline runs without a shipped model.
    def stardist_model_dir_arg = params.segmentation_model_dir ? "--model-dir ${params.segmentation_model_dir}" : ''
    // InstanSeg writes its BioImage.IO model under INSTANSEG_BIOIMAGEIO_PATH.
    // The default (inside site-packages) is read-only under Singularity, so
    // we redirect to a host-mounted path or, if unconfigured, to the task
    // work dir (writable; re-downloads per task).
    def instanseg_cache_dir = params.instanseg_model_dir ?: "\$PWD/.instanseg_cache"
    // CellSAM flags. use_wsi toggles native tiling; model_path points at
    // pre-downloaded weights (omit -> auto-download, needs DEEPCELL_ACCESS_TOKEN).
    def cellsam_wsi_flag = params.seg_cellsam_use_wsi ? '--use-wsi' : ''
    def cellsam_model_arg = params.cellsam_model_path ? "--model-path ${params.cellsam_model_path}" : ''
    // CellSAM extracts ONE nuclear channel (it is NOT channel-invariant like
    // InstanSeg), so resolve the real DAPI index from meta.channels instead of
    // assuming channel 0. -1 when absent; guarded in the script block below.
    def cellsam_dapi_idx = (meta.channels ?: []).findIndexOf { it.toString().toUpperCase() == 'DAPI' }

    if (params.seg_method == 'cellsam') {
        """
        # Log input size for tracing (-L follows symlinks)
        input_bytes=\$(stat -L --printf="%s" ${merged_file} 2>/dev/null || echo 0)
        echo "${task.process},${meta.patient_id},${merged_file.name},\${input_bytes}" > ${meta.patient_id}.SEGMENT.size.csv

        echo "Sample: ${meta.patient_id}"
        echo "Backend: CellSAM (bbox_threshold=${params.seg_cellsam_bbox_threshold}, use_wsi=${params.seg_cellsam_use_wsi})"
        echo "Note: CellSAM segments the nuclear (DAPI) channel resolved to index ${cellsam_dapi_idx}; cell mask is expanded from nuclei."

        # CellSAM is NOT channel-invariant: it segments a single nuclear channel.
        # Fail loudly when no DAPI channel exists rather than silently segmenting
        # the wrong channel (a negative index would wrap around under NumPy).
        if [ "${cellsam_dapi_idx}" -lt 0 ]; then
            echo "ERROR: no 'DAPI' channel found in [${meta.channels ? meta.channels.join(', ') : 'Unknown'}] for ${meta.patient_id}"
            echo "CellSAM requires a nuclear (DAPI) channel to be present."
            exit 1
        fi

        # Warn early if auto-download will be attempted without credentials.
        if [ -z "${cellsam_model_arg}" ] && [ -z "\${DEEPCELL_ACCESS_TOKEN:-}" ]; then
            echo "WARNING: --model-path unset and DEEPCELL_ACCESS_TOKEN is empty; CellSAM weight auto-download will likely fail."
        fi

        segment_cellsam.py \\
            --image ${merged_file} \\
            --output-dir . \\
            --dapi-channel ${cellsam_dapi_idx} \\
            --expand-distance ${params.seg_expand_distance} \\
            --bbox-threshold ${params.seg_cellsam_bbox_threshold} \\
            --block-size ${params.seg_cellsam_block_size} \\
            --overlap ${params.seg_cellsam_overlap} \\
            --prefix ${prefix} \\
            ${cellsam_wsi_flag} \\
            ${cellsam_model_arg} \\
            ${use_gpu_flag} \\
            ${args}

        cat <<-END_VERSIONS > versions.yml
        "${task.process}":
            python: \$(python --version 2>&1 | sed 's/Python //')
            cellSAM: \$(python -c "import cellSAM; print(getattr(cellSAM, '__version__', 'unknown'))" 2>/dev/null || echo "unknown")
            torch: \$(python -c "import torch; print(torch.__version__)" 2>/dev/null || echo "unknown")
        END_VERSIONS
        """
    } else if (params.seg_method == 'instantseg') {
        """
        # Log input size for tracing (-L follows symlinks)
        input_bytes=\$(stat -L --printf="%s" ${merged_file} 2>/dev/null || echo 0)
        echo "${task.process},${meta.patient_id},${merged_file.name},\${input_bytes}" > ${meta.patient_id}.SEGMENT.size.csv

        # Redirect InstanSeg model cache away from the read-only container image.
        export INSTANSEG_BIOIMAGEIO_PATH="${instanseg_cache_dir}"
        mkdir -p "\$INSTANSEG_BIOIMAGEIO_PATH"

        echo "Sample: ${meta.patient_id}"
        echo "Backend: InstanSeg (model=${params.seg_instantseg_model}, target=${params.seg_instantseg_target})"
        echo "InstanSeg model cache: \$INSTANSEG_BIOIMAGEIO_PATH"
        echo "Note: InstanSeg is channel-invariant; skipping DAPI-channel-0 check."

        segment_instantseg.py \\
            --image ${merged_file} \\
            --output-dir . \\
            --model-name ${params.seg_instantseg_model} \\
            --target ${params.seg_instantseg_target} \\
            --pixel-size ${params.pixel_size} \\
            --prefix ${prefix} \\
            --tile-size ${params.seg_instantseg_tile_size} \\
            --batch-size ${params.seg_instantseg_batch_size} \\
            ${use_gpu_flag} \\
            ${args}

        cat <<-END_VERSIONS > versions.yml
        "${task.process}":
            python: \$(python --version 2>&1 | sed 's/Python //')
            instanseg: \$(python -c "import instanseg; print(getattr(instanseg, '__version__', 'unknown'))" 2>/dev/null || echo "unknown")
            torch: \$(python -c "import torch; print(torch.__version__)" 2>/dev/null || echo "unknown")
        END_VERSIONS
        """
    } else {
        """
        # Log input size for tracing (-L follows symlinks)
        input_bytes=\$(stat -L --printf="%s" ${merged_file} 2>/dev/null || echo 0)
        echo "${task.process},${meta.patient_id},${merged_file.name},\${input_bytes}" > ${meta.patient_id}.SEGMENT.size.csv

        echo "Sample: ${meta.patient_id}"
        echo "Attempt: ${task.attempt} - Using n_tiles_y=${n_tiles_y}, n_tiles_x=${n_tiles_x}"

        # Runtime validation: DAPI must be in channel 0 for segmentation
        if [ "${dapi_check}" != "DAPI" ]; then
            echo "ERROR: DAPI must be in channel 0 for segmentation"
            echo "Found channels: ${meta.channels ? meta.channels.join(', ') : 'Unknown'}"
            echo "Channel 0: ${meta.channels ? meta.channels[0] : 'Unknown'}"
            exit 1
        fi
        echo "Validated: DAPI is in channel 0"

        segment.py \\
            --image ${merged_file} \\
            --output-dir . \\
            ${stardist_model_dir_arg} \\
            --model-name ${params.segmentation_model} \\
            --dapi-channel 0 \\
            --n-tiles ${n_tiles_y} ${n_tiles_x} \\
            --expand-distance ${params.seg_expand_distance} \\
            --pmin ${pmin} \\
            --pmax ${pmax} \\
            ${use_gpu_flag} \\
            ${args}

        cat <<-END_VERSIONS > versions.yml
        "${task.process}":
            python: \$(python --version 2>&1 | sed 's/Python //')
            deepcell: \$(python -c "import deepcell; print(deepcell.__version__)" 2>/dev/null || echo "unknown")
            tensorflow: \$(python -c "import tensorflow; print(tensorflow.__version__)" 2>/dev/null || echo "unknown")
        END_VERSIONS
        """
    }

    stub:
    def prefix = task.ext.prefix ?: "${meta.patient_id}"
    """
    touch ${prefix}_nuclei_mask.tif
    touch ${prefix}_cell_mask.tif
    echo "STUB,${meta.patient_id},stub,0" > ${meta.patient_id}.SEGMENT.size.csv

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        seg_method: ${params.seg_method}
    END_VERSIONS
    """
}
