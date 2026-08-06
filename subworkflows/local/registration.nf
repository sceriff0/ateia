/*
========================================================================================
    IMPORT MODULES
========================================================================================
*/

include { GENERATE_REGISTRATION_QC          } from '../../modules/local/generate_registration_qc'

include { SEG_QC                            } from './seg_qc'

include { VALIS_ADAPTER                     } from './adapters/valis_adapter'
include { TILED_ADAPTER                     } from './adapters/tiled_adapter'


/*
========================================================================================
    SUBWORKFLOW: REGISTRATION
========================================================================================
    Configuration:
        - params.skip_registration_qc: true | false (skip QC generation)
        - params.qc_scale_factor: float (QC downsampling factor, default 0.25)
        - method: 'valis'

    Input:
        ch_preprocessed: Channel of [meta, file] tuples
        method: Registration method name

    Output:
        registered: Channel of [meta, file] tuples (standard format)
        qc: Channel of QC outputs (PNG and TIFF)
        checkpoint_csv: Checkpoint CSV file

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
    // STEP 1: Images enter registration as-is. Both backends align inputs of
    // differing sizes natively (VALIS resolves them into a shared space; the
    // tiled/STARE backend warps each moving slide into the reference's shape),
    // so no common-canvas padding step is needed.
    // ========================================================================
    ch_images = ch_preprocessed

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

    // Flattened back to [meta, file] for consumers (seg-QC) that segment individual slides
    // rather than patient groups. Single-slide patients are excluded here on purpose: they
    // have no moving slide to score against, so segmenting their reference would compute a
    // full GPU StarDist WSI segmentation and discard it.
    //
    // NOTE: unlike the raw ch_preprocessed stream, `items` here comes out of the grouping
    // closure above, so under allow_auto_reference=true it carries that closure's
    // is_reference: true fill-in (registration.nf:100) for patients whose CSV marked no
    // reference. seg_qc.nf's reference/moving branch reads meta.is_reference, so this
    // channel is what lets an auto-reference multi-slide patient be scored at all — sourced
    // from the raw stream, that patient's reference branch was empty and it silently
    // produced zero seg-QC output.
    ch_images_multi = ch_grouped_multi.flatMap { pid, ref, items -> items }

    // ========================================================================
    // STEP 3: RUN REGISTRATION VIA METHOD-SPECIFIC ADAPTER
    // ========================================================================
    // Each adapter:
    //   - Takes: ch_grouped (patient-grouped structure with references identified)
    //   - Converts to method-specific format
    //   - Runs registration
    //   - Converts output back to [meta, file] standard format

    // Default registration runs via the classic monolithic VALIS adapter (single-process
    // REGISTER). NOTE: the *old distributed-VALIS* low-memory path was archived 2026-07-24
    // (git tag archive/tiled-valis-2026-07-24). The current `registration_method='tiled'`
    // is a SEPARATE, live STARE backend dispatched via TILED_ADAPTER below — not that
    // archived path. Don't confuse the two.
    //
    // Registrar pickle (per patient) for the GeoJSON seg-QC, and the pre-micro displacement
    // fields (only when reg_qc >= 2 asked REGISTER for them), both come from classic VALIS.
    ch_registrar_pickle = Channel.empty()
    ch_stage_checkpoint = Channel.empty()

    // reg_qc: 0 = none, 1 = DAPI overlay, 2 = + segmentation overlap (legacy
    // skip_registration_qc=true forces 0). ParamUtils.regQcLevel is the single definition.
    def reg_qc_level = ParamUtils.regQcLevel(params)

    // Level 2 warps polygons through the registrar the method produced — the VALIS pickle (via the
    // BioFormats JVM) or the STARE manifest (JVM-free). The scorer is identical; only the warper
    // differs (WARP_SEG_QC vs WARP_SEG_QC_TILED). Segmentation of the native slides is shared.
    def do_seg_qc = reg_qc_level >= 2

    // Per-moving-slide STARE manifest (meta-keyed), used by the tiled reg_qc=2 seg-QC join.
    ch_manifest_by_meta = Channel.empty()

    // Dispatch on the registration method. Both adapters emit the identical channel contract,
    // so everything downstream (QC, checkpoint, error estimation) is method-agnostic.
    if (params.registration_method == 'tiled') {
        TILED_ADAPTER(ch_grouped_multi)
        ch_registered       = TILED_ADAPTER.out.registered
        ch_registrar_pickle = TILED_ADAPTER.out.manifest
        ch_manifest_by_meta = TILED_ADAPTER.out.manifest_by_meta
        ch_stage_checkpoint = TILED_ADAPTER.out.stage_checkpoint
        ch_adapter_logs     = TILED_ADAPTER.out.size_logs
        ch_adapter_versions = TILED_ADAPTER.out.versions
        ch_adapter_summary  = TILED_ADAPTER.out.summary
    } else {
        VALIS_ADAPTER(ch_grouped_multi)
        ch_registered       = VALIS_ADAPTER.out.registered
        ch_registrar_pickle = VALIS_ADAPTER.out.registrar
        ch_stage_checkpoint = VALIS_ADAPTER.out.stage_checkpoint
        ch_adapter_logs     = VALIS_ADAPTER.out.size_logs
        ch_adapter_versions = VALIS_ADAPTER.out.versions
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
    // Per-cell registration residuals (final stage), for the SpatialData export.
    ch_seg_residuals = Channel.empty()
    // Captured from whichever WARP process the method dispatches to, so the version/size-log
    // aggregation below never references a process that was not invoked in this run.
    ch_seg_qc_size_log = Channel.empty()
    ch_seg_qc_versions = Channel.empty()
    // The whole block (segment natives -> branch -> checkpoint totalisation -> method-specific
    // warp) lives in subworkflows/local/seg_qc.nf, shared with add_cycle.nf. The valis/tiled
    // dispatch is SEG_QC's business, so nothing here reaches back through the adapter seam:
    // the tiled-only and valis-only channels are handed over together and SEG_QC reads the
    // ones its branch needs (the other side is Channel.empty() and is never consumed).
    if (do_seg_qc) {
        SEG_QC(ch_images_multi, ch_registrar_pickle, ch_stage_checkpoint, ch_manifest_by_meta)
        ch_seg_qc          = SEG_QC.out.metrics
        ch_seg_residuals   = SEG_QC.out.per_cell
        ch_seg_qc_size_log = SEG_QC.out.size_log
        ch_seg_qc_versions = SEG_QC.out.versions
    }

    // ========================================================================
    // STEP 4: CHECKPOINT
    // ========================================================================
    // Use collectFile() for non-blocking aggregation (enables patient-level parallelism)
    ch_checkpoint_csv = ch_registered
        .map { meta, file ->
            // Where the file WILL be published. This must agree with REGISTER's /
            // TILED_*'s publishDir in conf/modules.config, including the producer
            // subdirectory those blocks' `pattern:` carries along
            // ('registered_slides/' for VALIS, 'registered/' for tiled, none for a
            // reference that passes through unregistered). That rule -- and the
            // work-hash test that recovers the subdirectory -- now lives in
            // Layout.publishedPathWithProducerSubdir, which documents why it is a
            // heuristic and what it costs to replace.
            def published_path = Layout.publishedPathWithProducerSubdir(
                params.outdir, meta.patient_id, Layout.REGISTERED, file)
            "${meta.patient_id},${published_path},${meta.is_reference},${meta.channels.join('|')}"
        }
        .collectFile(
            name: Layout.checkpointCsvName(Layout.REGISTERED),
            newLine: true,
            storeDir: Layout.checkpointDir(params.outdir),
            seed: 'patient_id,registered_image,is_reference,channels'
        )

    // Collect size logs from all registration processes
    ch_size_logs = Channel.empty()

    // Add size logs from the registration adapter
    ch_size_logs = ch_size_logs.mix(ch_adapter_logs)

    // Add size logs from QC processes (if enabled)
    if (reg_qc_level >= 1) {
        ch_size_logs = ch_size_logs.mix(GENERATE_REGISTRATION_QC.out.size_log)
    }
    // SEG_QC.out.size_log already carries SEG_QC_GEOJSON's log plus the warp process's.
    if (do_seg_qc) {
        ch_size_logs = ch_size_logs.mix(ch_seg_qc_size_log)
    }

    // Collect versions from all registration processes
    ch_versions = Channel.empty()

    ch_versions = ch_versions.mix(ch_adapter_versions)

    if (reg_qc_level >= 1) {
        ch_versions = ch_versions.mix(GENERATE_REGISTRATION_QC.out.versions.first())
    }
    // SEG_QC.out.versions is already de-duplicated per process (.first() applied inside).
    if (do_seg_qc) {
        ch_versions = ch_versions.mix(ch_seg_qc_versions)
    }

    emit:
    registered       = ch_registered
    checkpoint_csv   = ch_checkpoint_csv
    qc               = ch_qc
    seg_qc           = ch_seg_qc
    seg_residuals    = ch_seg_residuals
    valis_summary    = ch_adapter_summary
    size_logs        = ch_size_logs
    versions         = ch_versions
}
