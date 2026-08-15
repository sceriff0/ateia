/*
========================================================================================
    IMPORT MODULES
========================================================================================
*/

include { GENERATE_REGISTRATION_QC          } from '../../modules/local/generate_registration_qc'

include { SEG_QC                            } from './seg_qc'

// The passthrough branch, the VALIS/tiled dispatch and the checkpoint manifest are
// REGISTER_PATIENT's, shared with add_cycle.nf.
include { REGISTER_PATIENT                  } from './register_patient'


/*
========================================================================================
    SUBWORKFLOW: REGISTRATION
========================================================================================
    Configuration:
        - params.skip_registration_qc: true | false (skip QC generation)
        - params.qc_scale_factor: float (QC downsampling factor, default 0.25)
        - params.registration_method: 'valis' | 'tiled' -- read ONCE here, the single
          decision site on this path, and handed as an ARGUMENT to both REGISTER_PATIENT
          (which owns the adapter dispatch) and SEG_QC (which owns the warp dispatch).
          Neither reads the global, so neither can be what lifts add_cycle's VALIS-only
          guarantee.

    Input:
        ch_preprocessed: Channel of [meta, file] tuples

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
    // Output: [patient_id, reference_item, all_items]
    //   where reference_item = [meta, file]
    //   and all_items = [[meta1, file1], [meta2, file2], ...]
    //
    // The sized groupKey, remainder:true, the GroupKey unwrap and the canonical
    // ordering are lib/PatientGroup.groovy's -- read its header for what each one
    // prevents. What is local to this site is only WHICH count sizes the group
    // (images_count: one item per slide) and WHAT orders it (meta.id, the
    // patient-prefixed source-image stem, which is unique per slide).
    ch_grouped = PatientGroup.byPatient(
            ch_images,
            name  : 'REGISTRATION: the per-patient slide group feeding REGISTER',
            size  : 'images_count',
            sortBy: { meta, _file -> meta.id },
        )
        .map { patient_id, items ->
            // The reference was RESOLVED at samplesheet-read time (INPUT_CHECK, via
            // CsvUtils.resolveReferenceRows) and travels in meta.is_reference. This is
            // a guard on that invariant, not a fallback: it used to promote items[0]
            // here, which meant the reference was whichever slide arrived first AND was
            // decided downstream of the preprocessing checkpoint writer, so an
            // --allow_auto_reference run wrote csv/preprocessed.csv with is_reference
            // false on every row and its own `--start registration` then refused to
            // read it. Reaching this error now means the samplesheet had no reference
            // and auto-promotion did not apply -- at any entry point after
            // preprocessing that is deliberate (ParamUtils.autoReferenceAllowed): a
            // checkpoint missing its reference is corrupt, and inferring one would
            // register against a different slide than the run that produced it.
            def ref = items.find { item -> item[0].is_reference }
            if (!ref) {
                throw new Exception("""
                No reference image found for patient ${patient_id}
                Fix: Set is_reference=true for one image in your input CSV
                OR, at --start preprocessing only, set allow_auto_reference=true
                """.stripIndent())
            }

            [patient_id, ref, items]
        }

    // ========================================================================
    // STEP 3: REGISTER EACH PATIENT GROUP
    // ========================================================================
    // The single-slide passthrough branch, the method dispatch (VALIS / tiled) and
    // the checkpoint manifest all live in REGISTER_PATIENT, which add_cycle.nf also
    // calls — it used to reimplement them, and had lost two of the three.
    //
    // The GROUPING above stays here: the linear path's sized groupKey and its
    // allow_auto_reference resolution have no counterpart on the add_cycle path,
    // whose reference is the frozen prior one.
    // The ONE read of params.registration_method on this path. Bound once and passed as an
    // argument to both consumers, so there is a single decision site instead of two reads
    // that could drift apart.
    def registration_method = params.registration_method
    // ... and resolved ONCE into the backend's declared emit cardinalities. Downstream, the
    // NAME selects an implementation (which adapter workflow to call, which warp container)
    // while the CONTRACT selects a data shape (which join `transform` needs). Keeping those
    // two uses apart is the point: a third backend that fills `transform` per slide gets the
    // per-slide join from its declaration, instead of from someone remembering to widen an
    // `== 'tiled'` test in seg_qc.nf. of() refuses a method it has never heard of.
    def registration_contract = AdapterContract.of(registration_method)

    REGISTER_PATIENT(ch_grouped, registration_method)

    ch_registered         = REGISTER_PATIENT.out.registered
    ch_images_multi       = REGISTER_PATIENT.out.images_multi
    // The method's transform, per patient (VALIS registrar pickle / STARE manifest) and --
    // where the method has one -- per moving slide. The unfilled one is Channel.empty().
    ch_transform          = REGISTER_PATIENT.out.transform
    ch_transform_by_slide = REGISTER_PATIENT.out.transform_by_slide
    ch_stage_checkpoint   = REGISTER_PATIENT.out.stage_checkpoint
    ch_adapter_logs       = REGISTER_PATIENT.out.size_logs
    ch_adapter_versions   = REGISTER_PATIENT.out.versions
    // The method's OWN target-registration-error estimate. Both backends produce one
    // (VALIS a feature-distance CSV, STARE a *_tre.json); a backend that produced none
    // would emit Channel.empty() here and every consumer must tolerate that.
    ch_registration_tre   = REGISTER_PATIENT.out.intrinsic_tre

    // reg_qc: 0 = none, 1 = DAPI overlay, 2 = + segmentation overlap (legacy
    // skip_registration_qc=true forces 0). ParamUtils.regQcLevel is the single definition.
    def reg_qc_level = ParamUtils.regQcLevel(params)

    // Level 2 warps polygons through the registrar the method produced — the VALIS pickle (via the
    // BioFormats JVM) or the STARE manifest (JVM-free). The scorer is identical; only the warper
    // differs (WARP_SEG_QC's two backends, keyed by lib/WarpBackends.groovy). Segmentation of
    // the native slides is shared.
    def do_seg_qc = reg_qc_level >= 2

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

    // Level >= 2: GeoJSON segmentation-overlap QC (per-pair IoU, dice_matched, centroid
    // residuals) BEFORE vs AFTER.
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
    // warp) lives in subworkflows/local/seg_qc.nf, shared with add_cycle.nf. Both transform
    // channels are handed over under the adapters' shared names; whichever one the method does
    // not produce is Channel.empty() (the adapters' null-object contract) and is never read.
    // The contract reaches SEG_QC as an ARGUMENT — derived from the same single read
    // REGISTER_PATIENT's method came from.
    if (do_seg_qc) {
        SEG_QC(ch_images_multi, ch_transform, ch_stage_checkpoint, ch_transform_by_slide,
               registration_contract)
        ch_seg_qc          = SEG_QC.out.metrics
        ch_seg_residuals   = SEG_QC.out.per_cell
        ch_seg_qc_size_log = SEG_QC.out.size_log
        ch_seg_qc_versions = SEG_QC.out.versions
    }

    // ========================================================================
    // STEP 4: CHECKPOINT
    // ========================================================================
    // Written by REGISTER_PATIENT (via REGISTERED_CHECKPOINT), so the add_cycle path
    // — which does not come through here — produces the identical manifest.
    ch_checkpoint_csv = REGISTER_PATIENT.out.checkpoint_csv

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
    registration_tre = ch_registration_tre
    size_logs        = ch_size_logs
    versions         = ch_versions
}
