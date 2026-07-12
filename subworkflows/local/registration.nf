/*
========================================================================================
    IMPORT MODULES
========================================================================================
*/

include { GET_IMAGE_DIMS                    } from '../../modules/local/get_image_dims'
include { MAX_DIM                           } from '../../modules/local/max_dim'
include { PAD_IMAGES                        } from '../../modules/local/pad_images'
include { GENERATE_REGISTRATION_QC          } from '../../modules/local/generate_registration_qc'
include { SEG_QC_GEOJSON                    } from '../../modules/local/seg_qc_geojson'
include { WARP_SEG_QC                       } from '../../modules/local/warp_seg_qc'

include { VALIS_ADAPTER                     } from './adapters/valis_adapter'
include { VALIS_DISTRIBUTED_ADAPTER         } from './adapters/valis_distributed_adapter'
include { REG_ESTIMATE                      } from '../../modules/local/reg_estimate'

include { ESTIMATE_FEATURE_DISTANCES        } from '../../modules/local/estimate_feature_distances'


/*
========================================================================================
    SUBWORKFLOW: REGISTRATION
========================================================================================
    Configuration:
        - params.padding: true | false (optional padding per patient)
        - params.skip_registration_qc: true | false (skip QC generation)
        - params.qc_scale_factor: float (QC downsampling factor, default 0.25)
        - params.enable_feature_error: true | false (enable feature-based TRE)
        - method: 'valis'

    Input:
        ch_preprocessed: Channel of [meta, file] tuples
        method: Registration method name

    Output:
        registered: Channel of [meta, file] tuples (standard format)
        qc: Channel of QC outputs (PNG and TIFF)
        checkpoint_csv: Checkpoint CSV file
        error_metrics: Channel of error estimation outputs (optional)

    QC Generation:
        - Decoupled from registration methods
        - Uses unified GENERATE_REGISTRATION_QC module
        - Compares registered vs reference DAPI channels
        - Outputs: full-res TIFF + downsampled PNG

    Error Estimation (Optional):
        - BEFORE registration: COMPUTE_FEATURES extracts matched keypoints
        - AFTER registration: Two complementary methods:
          1. Feature-based TRE (fast, sparse measurements)
          2. Segmentation-based IoU/Dice (dense, biologically meaningful)
========================================================================================
*/

workflow REGISTRATION {
    take:
    ch_preprocessed

    main:
    // ========================================================================
    // STEP 1: OPTIONAL PADDING (per patient)
    // ========================================================================
    if (params.padding) {
        // Get dimensions for all images
        GET_IMAGE_DIMS(ch_preprocessed)

        // Group by patient and find max dimensions per patient
        // Use groupKey for streaming - emits as soon as images_count items collected
        ch_grouped_dims = GET_IMAGE_DIMS.out.dims
            .map { meta, dims ->
                def key = meta.images_count ? groupKey(meta.patient_id, meta.images_count) : meta.patient_id
                [key, dims]
            }
            .groupTuple()
            .map { key, dims_list ->
                [ [patient_id: key.toString()], dims_list ]
            }

        MAX_DIM(ch_grouped_dims)

        // MAX_DIM outputs [meta, max_dims_file]
        // Combine each individual image with its patient's max_dims_file
        ch_to_pad = ch_preprocessed
            .map { meta, file -> [meta.patient_id, meta, file] }
            .combine(MAX_DIM.out.max_dims_file.map { meta, f -> [meta.patient_id, f] }, by: 0)
            .map { patient_id, meta, file, max_dims -> [meta, file, max_dims] }

        PAD_IMAGES(ch_to_pad)
        ch_images = PAD_IMAGES.out.padded
        ch_images_for_error = ch_images.map { it }
    } else {
        ch_images = ch_preprocessed
        ch_images_for_error = ch_images.map { it }
    }

    // ========================================================================
    // STEP 2: GROUP BY PATIENT AND IDENTIFY REFERENCES
    // ========================================================================
    // This is common preparation needed by all methods
    // Output: [patient_id, reference_item, all_items]
    //   where reference_item = [meta, file]
    //   and all_items = [[meta1, file1], [meta2, file2], ...]
    //
    // Use groupKey for streaming - emits as soon as images_count items collected
    // This enables patient-level parallelism (Patient A processes while Patient B preprocesses)

    ch_grouped = ch_images
        .map { meta, file ->
            def key = meta.images_count ? groupKey(meta.patient_id, meta.images_count) : meta.patient_id
            [key, meta, file]
        }
        .groupTuple()
        .map { patient_id, metas, files ->
            // Combine metas and files into items
            def items = [metas, files].transpose()

            // Find reference image
            def ref = items.find { item -> item[0].is_reference }

            if (!ref) {
                if (params.allow_auto_reference) {
                    log.warn """
                    WARNING: No reference marked for patient ${patient_id}
                    Using first image as reference (allow_auto_reference=true)
                    To make this an error, set allow_auto_reference=false
                    """.stripIndent()
                    // Mark the auto-picked image as the reference so downstream
                    // steps (registration QC branch, segmentation's is_reference
                    // filter) see exactly one reference — otherwise the registered
                    // output carries is_reference=false for every image and
                    // postprocessing fails with "No reference images found".
                    items[0] = [items[0][0] + [is_reference: true], items[0][1]]
                    ref = items[0]
                } else {
                    throw new Exception("""
                    No reference image found for patient ${patient_id}
                    Fix: Set is_reference=true for one image in your input CSV
                    OR set allow_auto_reference=true to use first image automatically
                    """.stripIndent())
                }
            }

            [patient_id, ref, items]
        }

    // Single-slide patients (only the reference, nothing to register) must NOT
    // be sent to the registration adapter: VALIS crashes on a lone image
    // ("negative dimensions are not allowed" / "M is None — no transformation
    // matrix"). For such a patient the reference IS the registered output, so we
    // branch it out here and pass it straight through to ch_registered below.
    ch_grouped_split = ch_grouped.branch { pid, ref, items ->
        single: items.size() == 1
        multi:  true
    }
    ch_grouped_multi = ch_grouped_split.multi
    ch_passthrough   = ch_grouped_split.single.map { pid, ref, items -> ref }  // [meta, file]

    // ========================================================================
    // STEP 3: RUN REGISTRATION VIA METHOD-SPECIFIC ADAPTER
    // ========================================================================
    // Each adapter:
    //   - Takes: ch_grouped (patient-grouped structure with references identified)
    //   - Converts to method-specific format
    //   - Runs registration
    //   - Converts output back to [meta, file] standard format

    // Opt-in distributed tiled registration (spec §6.5/§6.6): lifts VALIS's non-rigid tile loop into
    // REG_PREP -> REG_TILE (fan-out) -> REG_FINALIZE (fan-in) for low-RAM clusters. Bit-identical to
    // VALIS's own tiler on its processed 2-D images. Default (reg_distributed_tiling=false) = classic.
    //
    // IMPORTANT (spec §6.5): tiled non-rigid != whole-image non-rigid. VALIS only tiles when
    // est_GB > threshold (rarely, since non-rigid runs downsampled). So:
    //   - 'auto' (default): REG_ESTIMATE per patient -> route est>thr to distributed (== classic-tiled),
    //     est<=thr to classic whole-image (IDENTICAL to classic). This keeps <10GB inputs bit-identical.
    //   - 'force': always tile (RAM win at the cost of differing from classic-whole-image for small data).
    // Registrar pickle (per patient) for the GeoJSON seg-QC — classic VALIS only
    // (the distributed path produces no single registrar pickle).
    ch_registrar_pickle = Channel.empty()

    if (!params.reg_distributed_tiling) {
        VALIS_ADAPTER(ch_grouped_multi)
        ch_registered       = VALIS_ADAPTER.out.registered
        ch_registrar_pickle = VALIS_ADAPTER.out.registrar
        ch_adapter_logs     = VALIS_ADAPTER.out.size_logs
        ch_adapter_versions = VALIS_ADAPTER.out.versions
        ch_adapter_summary  = VALIS_ADAPTER.out.summary
    } else if ((params.reg_dist_sub_threshold ?: 'auto') == 'force') {
        VALIS_DISTRIBUTED_ADAPTER(ch_grouped_multi)
        ch_registered       = VALIS_DISTRIBUTED_ADAPTER.out.registered
        ch_adapter_logs     = VALIS_DISTRIBUTED_ADAPTER.out.size_logs
        ch_adapter_versions = VALIS_DISTRIBUTED_ADAPTER.out.versions
        ch_adapter_summary  = Channel.empty()
    } else {
        // 'auto': route per patient by INPUT SIZE (spec §6.7) — large inputs (JVM-RAM concern) ->
        // distributed (JVM-free, bit-identical); small inputs -> classic VALIS (monolithic is fine).
        REG_ESTIMATE(ch_grouped_multi.map { pid, ref_item, all_items ->
            tuple(pid, ref_item[1], all_items.collect { it[1] })
        })
        ch_routed = ch_grouped_multi
            .map { pid, ref_item, all_items -> tuple(pid, [ref_item, all_items]) }
            .join(REG_ESTIMATE.out.decision)
            .branch { pid, payload, use_distributed ->
                distributed: use_distributed == 'true'
                classic:     true
            }
        VALIS_DISTRIBUTED_ADAPTER(ch_routed.distributed.map { pid, payload, u -> tuple(pid, payload[0], payload[1]) })
        VALIS_ADAPTER(            ch_routed.classic.map     { pid, payload, u -> tuple(pid, payload[0], payload[1]) })
        ch_registrar_pickle = VALIS_ADAPTER.out.registrar   // only classic slides have a pickle
        ch_registered       = VALIS_DISTRIBUTED_ADAPTER.out.registered.mix(VALIS_ADAPTER.out.registered)
        ch_adapter_logs     = VALIS_DISTRIBUTED_ADAPTER.out.size_logs.mix(VALIS_ADAPTER.out.size_logs)
        ch_adapter_versions = VALIS_DISTRIBUTED_ADAPTER.out.versions.mix(VALIS_ADAPTER.out.versions)
        ch_adapter_summary  = VALIS_ADAPTER.out.summary
    }

    // Re-introduce single-slide patients (reference passed through unregistered)
    // into the registered stream for QC, checkpointing and postprocessing.
    ch_registered = ch_registered.mix(ch_passthrough)

    // ========================================================================
    // STEP 3b: GENERATE QC (Method-independent)
    // ========================================================================
    // For each registered image, create QC comparing it to its reference
    // This is now decoupled from the registration method

    // Prepare input for QC: [meta, registered_file, reference_file]
    ch_qc_input = ch_registered
        .branch {
            reference: it[0].is_reference
            moving: !it[0].is_reference
        }

    // Extract references by patient
    ch_references_for_qc = ch_qc_input.reference
        .map { meta, file -> [meta.patient_id, file] }

    // Combine moving images with their patient's reference (1 reference to N moving images)
    ch_for_qc = ch_qc_input.moving
        .map { meta, file -> [meta.patient_id, meta, file] }
        .combine(ch_references_for_qc, by: 0)
        .map { patient_id, meta, registered_file, reference_file ->
            [meta, registered_file, reference_file]
        }

    // reg_qc controls registration QC depth: 0 = none, 1 = DAPI overlay only,
    // 2 = DAPI overlay + segmentation-overlap metric. Legacy skip_registration_qc=true
    // forces 0 for backward compatibility.
    def reg_qc_level = params.skip_registration_qc ? 0 : (params.reg_qc == null ? 1 : (params.reg_qc as int))

    // Level >= 1: DAPI overlay image QC for all non-reference images
    if (reg_qc_level >= 1) {
        GENERATE_REGISTRATION_QC(ch_for_qc)
        ch_qc = GENERATE_REGISTRATION_QC.out.qc
    } else {
        ch_qc = Channel.empty()
    }

    // Level >= 2: GeoJSON segmentation-overlap QC (Dice/IoU/instance-F1) BEFORE vs AFTER.
    // Segment each slide's DAPI on its NATIVE (pre-registration) image -> cell GeoJSON,
    // then warp the polygons through the registrar and score overlap. Classic VALIS only
    // (needs the registrar pickle; distributed produces none).
    ch_seg_qc = Channel.empty()
    if (reg_qc_level >= 2) {
        SEG_QC_GEOJSON(ch_images_for_error)

        ch_gj = SEG_QC_GEOJSON.out.geojson.branch { meta, gj ->
            reference: meta.is_reference
            moving:    !meta.is_reference
        }
        // reference: [patient_id, ref_geojson, ref_slide_name(=stem)]
        ch_ref_gj = ch_gj.reference.map { meta, gj -> [meta.patient_id, gj, gj.simpleName] }
        // moving: [patient_id, meta, moving_geojson, moving_slide_name]
        ch_mov_gj = ch_gj.moving.map { meta, gj -> [meta.patient_id, meta, gj, gj.simpleName] }

        // Join each moving slide with its patient's reference GeoJSON + registrar pickle.
        ch_for_warp = ch_mov_gj
            .combine(ch_ref_gj, by: 0)
            .combine(ch_registrar_pickle, by: 0)
            .map { pid, meta, mov_gj, mov_name, ref_gj, ref_name, pickle ->
                tuple(meta, pickle, ref_name, mov_name, ref_gj, mov_gj)
            }

        WARP_SEG_QC(ch_for_warp)
        ch_seg_qc = WARP_SEG_QC.out.metrics
    }

    // ========================================================================
    // STEP 3C: ERROR ESTIMATION (Optional)
    // ========================================================================
    // For each non-reference image, measure quality by comparing:
    //   - reference vs moving (pre-registration)
    //   - reference vs registered (post-registration)

    ch_error_metrics = Channel.empty()

    if (params.enable_feature_error) {
        // For each non-reference image: [meta, reference, moving, registered]
        ch_for_error = ch_registered
            .filter { meta, file -> !meta.is_reference }
            .map { meta, reg_file -> [meta.patient_id, meta.channels.toSorted().join('|'), meta, reg_file] }
            .join(
                ch_images_for_error
                    .filter { meta, file -> !meta.is_reference }
                    .map { meta, mov_file -> [meta.patient_id, meta.channels.toSorted().join('|'), mov_file] },
                by: [0, 1]
            )
            .map { patient_id, channels, meta, reg_file, mov_file -> [patient_id, meta, reg_file, mov_file] }
            .combine(
                ch_images_for_error
                    .filter { meta, file -> meta.is_reference }
                    .map { meta, ref_file -> [meta.patient_id, ref_file] },
                by: 0
            )
            .map { patient_id, meta, reg_file, mov_file, ref_file ->
                tuple(meta, ref_file, mov_file, reg_file)
            }

        // Validate that we have images to process
        ch_for_error
            .count()
            .subscribe { n ->
                if (n == 0) {
                    log.warn "No images available for feature error estimation - check channel metadata consistency"
                } else {
                    log.info "Feature error estimation: processing ${n} image(s)"
                }
            }

        ESTIMATE_FEATURE_DISTANCES(ch_for_error)
        ch_error_metrics = ch_error_metrics.mix(ESTIMATE_FEATURE_DISTANCES.out.distance_metrics)
    }

    // ========================================================================
    // STEP 4: CHECKPOINT
    // ========================================================================
    // Use collectFile() for non-blocking aggregation (enables patient-level parallelism)
    ch_checkpoint_csv = ch_registered
        .map { meta, file ->
            // Construct the path where the file will be published
            // Must match the publishDir configuration in modules.config
            //
            // METHOD-AGNOSTIC APPROACH:
            // Detect if file is in a subdirectory by checking parent directory name length.
            // Nextflow work dirs are 32-char hex hashes (e.g., e6194a65f430c8860ff1f93c4a556c).
            // Real subdirectories (e.g., "registered_slides") have different lengths.
            //
            // - VALIS: work/.../registered_slides/file.tiff → parent="registered_slides" (17 chars)
            // - CPU/GPU: work/.../e6194a65f430c8860ff1f93c4a556c/file.tiff → parent=hash (32 chars)
            // - References: work/.../de93746794b82349b3fde77bf41502/file.tif → parent=hash (32 chars)

            def file_path = file instanceof List ? file[0] : file
            def filename = file_path.name
            def parent_name = file_path.parent?.name ?: ''

            // If parent name is NOT a Nextflow work hash (32 hex chars), it's a real subdirectory
            def is_work_hash = parent_name.length() == 32 && parent_name.matches(/^[0-9a-f]{32}$/)
            def relative_path = is_work_hash ? filename : "${parent_name}/${filename}"

            def published_path = "${params.outdir}/${meta.patient_id}/registered/${relative_path}"
            "${meta.patient_id},${published_path},${meta.is_reference},${meta.channels.join('|')}"
        }
        .collectFile(
            name: 'registered.csv',
            newLine: true,
            storeDir: "${params.outdir ?: launchDir}/csv",
            seed: 'patient_id,registered_image,is_reference,channels'
        )

    // Collect size logs from all registration processes
    ch_size_logs = Channel.empty()

    // Add size logs from padding processes (if padding is enabled)
    if (params.padding) {
        ch_size_logs = ch_size_logs
            .mix(GET_IMAGE_DIMS.out.size_log)
            .mix(PAD_IMAGES.out.size_log)
    }

    // Add size logs from the registration adapter
    ch_size_logs = ch_size_logs.mix(ch_adapter_logs)

    // Add size logs from QC processes (if enabled)
    if (reg_qc_level >= 1) {
        ch_size_logs = ch_size_logs.mix(GENERATE_REGISTRATION_QC.out.size_log)
    }
    if (reg_qc_level >= 2) {
        ch_size_logs = ch_size_logs
            .mix(SEG_QC_GEOJSON.out.size_log)
            .mix(WARP_SEG_QC.out.size_log)
    }

    // Add size logs from error estimation (if enabled)
    if (params.enable_feature_error) {
        ch_size_logs = ch_size_logs.mix(ESTIMATE_FEATURE_DISTANCES.out.size_log)
    }

    // Collect versions from all registration processes
    ch_versions = Channel.empty()

    if (params.padding) {
        ch_versions = ch_versions
            .mix(GET_IMAGE_DIMS.out.versions.first())
            .mix(PAD_IMAGES.out.versions.first())
    }

    ch_versions = ch_versions.mix(ch_adapter_versions)

    if (reg_qc_level >= 1) {
        ch_versions = ch_versions.mix(GENERATE_REGISTRATION_QC.out.versions.first())
    }
    if (reg_qc_level >= 2) {
        ch_versions = ch_versions
            .mix(SEG_QC_GEOJSON.out.versions.first())
            .mix(WARP_SEG_QC.out.versions.first())
    }
    if (params.enable_feature_error) {
        ch_versions = ch_versions.mix(ESTIMATE_FEATURE_DISTANCES.out.versions.first())
    }

    emit:
    registered       = ch_registered
    checkpoint_csv   = ch_checkpoint_csv
    qc               = ch_qc
    seg_qc           = ch_seg_qc
    error_metrics    = ch_error_metrics
    valis_summary    = ch_adapter_summary
    size_logs        = ch_size_logs
    versions         = ch_versions
}
