/*
 * REGISTER - VALIS whole-slide registration
 *
 * Performs multi-modal image registration using VALIS with SuperPoint/SuperGlue
 * feature detection. Supports rigid, non-rigid, and micro-registration stages.
 *
 * Input: Reference image path, preprocessed images, and metadata
 * Output: Registered OME-TIFF files aligned to reference coordinate space
 */
process REGISTER {
    // Uses patient_id (not meta.patient_id) because this is a fan-in process:
    // multiple per-slide metas are grouped into a single patient-level invocation.
    // patient_id is the grouping key; all_metas carries the per-slide metadata list.
    tag "${patient_id}"
    label 'process_high'

    // VALIS uses the maintained upstream image (linux/amd64); we do not rebuild
    // it (its from-source libvips build is heavy and not vendored). See containers/README.md.
    container 'cdgatenbee/valis-wsi:1.0.0'

    input:
    // Use stageAs to avoid filename collision when reference is included in preproc_files
    // meta carries patient_id for publishDir consistency across all processes
    tuple val(meta), val(patient_id), path(reference, stageAs: 'ref/*'), path(preproc_files, stageAs: 'input_?/*'), val(all_metas)

    output:
    tuple val(patient_id), path("registered_slides/*_registered.ome.tiff"), val(all_metas), path("channels_manifest.json"), emit: registered
    path "versions.yml"                                                                    , emit: versions
    path("*.size.csv")                                                                     , emit: size_log
    path("preprocessed/data/*.csv")                                                        , emit: summary, optional: true

    when:
    task.ext.when == null || task.ext.when

    script:
    def args = task.ext.args ?: ''
    // Extract reference filename from the staged path (ref/filename.tif)
    def ref_filename = reference ? reference.name : ''
    def ref_arg = ref_filename ? "--reference ${ref_filename}" :
                  params.reg_reference_markers ? "--reference-markers ${params.reg_reference_markers.join(' ')}" : ''
    // Memory mode controls feature detector, matcher, and dimension settings
    def memory_mode = params.memory_mode ?: 'high'
    def micro_reg_fraction = params.reg_micro_reg_fraction ?: 0.125
    def max_image_dim = params.reg_max_image_dim ?: 4000
    def skip_micro = params.skip_micro_registration ? '--skip-micro-registration' : ''
    // Performance options
    def parallel_warping = params.reg_parallel_warping ? '--parallel-warping' : ''
    def n_workers = params.reg_n_workers ?: 4
    // JVM heap scales with retry attempts: base 32GB + 16GB per attempt
    def jvm_heap_gb = Math.min(params.reg_jvm_heap_gb ?: (32 + 16 * task.attempt), task.memory.toGiga() - 4)
    // Advanced registration options
    def use_tiled = params.reg_use_tiled_registration ? '--use-tiled-registration' : ''
    def tile_size = params.reg_tile_size ?: 2048

    """
    # === LOG INPUT SIZES ===
    # Sum all input OME-TIFF file sizes for resource tracing
    total_bytes=\$(find -L ref input_* -maxdepth 1 -type f \\( -name "*.ome.tif" -o -name "*.ome.tiff" \\) -exec stat -L --printf="%s\\n" {} + 2>/dev/null | awk '{sum+=\$1} END {print sum}')
    echo "${task.process},${patient_id},inputs/,\${total_bytes:-0}" > ${patient_id}.REGISTER.size.csv

    mkdir -p registered_slides preprocessed

    # === PRINT REGISTRATION SETTINGS ===
    echo "========================================================================"
    echo "VALIS Registration - Attempt ${task.attempt}"
    echo "========================================================================"
    echo "Settings:"
    echo "  - memory_mode: ${memory_mode}"
    echo "  - max_image_dim: ${max_image_dim}"
    echo "  - skip_micro_registration: ${skip_micro ? 'YES' : 'NO'}"
    echo "========================================================================"

    # === STAGE INPUT FILES ===
    # Copy files (dereferencing symlinks) into preprocessed/ because VALIS
    # loses track of src_f when working with Nextflow symlinks.
    echo "=== Copying input files to preprocessed/ ==="

    # Collect all OME-TIFF files from staged ref/ and input_*/ directories
    find -L ref input_* -maxdepth 1 -type f \\( -name "*.ome.tif" -o -name "*.ome.tiff" \\) 2>/dev/null > /tmp/files_to_copy.txt || true

    echo "Files to copy:"
    cat /tmp/files_to_copy.txt

    # Parallel hard-link copy; cp -Ln skips duplicates (reference may also be in input files)
    cat /tmp/files_to_copy.txt | xargs -P ${task.cpus} -I {} sh -c '
        dest="preprocessed/\$(basename "{}")"
        cp -Ln "{}" "\$dest" 2>/dev/null && echo "Copied: {}" || echo "Skipped (already exists): {}"
    '

    echo "=== Contents of preprocessed/ ==="
    ls -lh preprocessed/

    # Verify we have actual files (not empty directory)
    file_count=\$(find preprocessed -type f -name '*.ome.tif*' | wc -l)
    echo "Total files copied: \$file_count"

    if [ "\$file_count" -eq 0 ]; then
        echo "ERROR: No .ome.tif files were copied to preprocessed/"
        echo "Available directories and contents:"
        ls -lR
        exit 1
    fi

    # === RUN VALIS REGISTRATION ===
    echo "=== Running registration ==="
    echo "Command: register.py --input-dir preprocessed --out registered_slides ${ref_arg}"

    register.py \\
        --input-dir preprocessed \\
        --out registered_slides \\
        ${ref_arg} \\
        --memory-mode ${memory_mode} \\
        --micro-reg-fraction ${micro_reg_fraction} \\
        --max-image-dim ${max_image_dim} \\
        ${skip_micro} \\
        ${parallel_warping} \\
        --n-workers ${n_workers} \\
        ${use_tiled} \\
        --tile-size ${tile_size} \\
        --jvm-heap-gb ${jvm_heap_gb} \\
        ${args}

    # === VALIDATE OUTPUTS ===
    echo "=== Contents of registered_slides/ ==="
    ls -lh registered_slides/ || echo "Directory is empty or doesn't exist"

    # Check that registration produced output files
    output_count=\$(find registered_slides -type f -name '*_registered.ome.tiff' 2>/dev/null | wc -l)
    echo "Total registered files created: \$output_count"

    if [ "\$output_count" -eq 0 ]; then
        echo "ERROR: No registered output files (*_registered.ome.tiff) were created"
        echo "Registration may have failed. Check the logs above."
        exit 1
    fi

    # Strict check: every input slide must produce a corresponding registered output
    if [ "\$output_count" -ne "\$file_count" ]; then
        echo "ERROR: Output count mismatch — expected \$file_count registered files but got \$output_count"
        echo "VALIS failed to warp some slides. Check the logs above for details."
        exit 1
    fi

    # === GENERATE CHANNELS MANIFEST ===
    # Extract channel names from OME-XML metadata of registered files.
    # convert_image guarantees OME-XML metadata is always present in pipeline images.
    echo "=== Creating channels manifest from OME metadata ==="
    create_channels_manifest.py \\
        --input-dir registered_slides \\
        --output channels_manifest.json

    # === RENAME SUMMARY CSVs WITH PATIENT ID ===
    # Prefix CSV names with patient_id to avoid collisions when aggregated in QC report
    for csv in preprocessed/data/*.csv; do
        if [ -f "\$csv" ]; then
            dir=\$(dirname "\$csv")
            base=\$(basename "\$csv" .csv)
            mv "\$csv" "\${dir}/${patient_id}_\${base}.csv"
        fi
    done

    # === CLEANUP INTERMEDIATES ===
    # Remove working copies to reclaim disk space. Do NOT delete staged inputs
    # (input_*, ref) — Nextflow needs them intact for retries.
    echo "=== Cleaning up intermediate files to save disk space ==="
    find preprocessed -maxdepth 1 -type f -delete
    rm -rf preprocessed/deformation_fields preprocessed/masks preprocessed/overlaps \
           preprocessed/rigid_registration preprocessed/non_rigid_registration preprocessed/processed

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: \$(python --version 2>&1 | sed 's/Python //')
        valis: \$(python -c "import valis; print(valis.__version__)" 2>/dev/null || echo "unknown")
    END_VERSIONS
    """

    stub:
    // Generate output files matching input count with proper naming pattern
    def output_files = all_metas.collect { m ->
        def markers = m.channels.join('_')
        "${patient_id}_${markers}_registered.ome.tiff"
    }
    def touch_commands = output_files.collect { "touch registered_slides/${it}" }.join('\n    ')
    // Build stub manifest JSON mapping filenames to their channel names
    def manifest_map = [all_metas, output_files].transpose().collectEntries { m, fname ->
        [(fname): m.channels]
    }
    def manifest_json = groovy.json.JsonOutput.toJson(manifest_map)
    """
    mkdir -p registered_slides
    ${touch_commands}
    echo '${manifest_json}' > channels_manifest.json
    echo "STUB,${patient_id},stub,0" > ${patient_id}.REGISTER.size.csv

    cat <<-END_VERSIONS > versions.yml
    "${task.process}":
        python: stub
        valis: stub
    END_VERSIONS
    """
}
