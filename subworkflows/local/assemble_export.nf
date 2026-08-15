/*
========================================================================================
    SUBWORKFLOW: ASSEMBLE_EXPORT
========================================================================================
    The final per-patient assembly, shared by the linear postprocessing path and the
    incremental add_cycle path:

      1. EXPORT_GEOJSON  — merged quantification + cell/nucleus contours (+ optional
                           phenotype extras) -> cells.geojson + cells_data.csv
      2. MERGE_AND_PYRAMID — per-patient channel TIFFs -> pyramidal OME-TIFF, with the
                           `embed_masks` gate deciding whether the segmentation masks
                           ride along as a second uint32 OME series.

    Both callers used to carry byte-identical copies of the export tuple assembly and
    of the `embed_masks` gate (comment included). They now exist once, here.

    THE INTERFACE IS A NAMED RECORD, NOT A POSITIONAL ARGUMENT LIST. `take:` used to be
    six channels, and two of them were tuples whose ARITY the callers had to keep in
    step by hand:

      - `ch_nuc_contours` was "real nucleus contours under --quantify_compartments, or
        the cell contours as a harmless placeholder", so both callers carried the same
        ternary;
      - `ch_pheno_extras` was `[patient_id, phenotypes, model_config, phenotype_qc]`,
        and add_cycle -- which has no PHENOTYPE stage at all -- carried a comment
        reading "the arity must track ASSEMBLE_EXPORT's take:" above a line that
        repeated the contours file three times.

    Both placeholder rules are declarations now (`when:` / `orElseField:` on the bundle
    below), stated once, here, where the consumer is. A caller passes the real channel
    or `Channel.empty()` and says whether the producer ran; it never builds a
    placeholder tuple whose shape it cannot check.

    One deliberate normalisation, kept from the first version of this file: the
    embed_masks gate joins the masks strictly. POSTPROCESSING's original copy used
    `join(by: 0)` and add_cycle's used `combine(by: 0)`; both channels carry exactly one
    entry per patient, so at the reachable cardinality they were equivalent — but a
    duplicate mask entry fans out silently under combine. lib/PatientArtifacts.groovy
    refuses it outright (failOnDuplicate), which is stricter than either.
========================================================================================
*/

include { EXPORT_GEOJSON    } from '../../modules/local/export_geojson'
include { MERGE_AND_PYRAMID } from '../../modules/local/merge_and_pyramid'

workflow ASSEMBLE_EXPORT {
    take:
    ch_artifacts        // PatientArtifacts.channels(..., PatientArtifacts.EXPORT_FIELDS, ...)
                        //   merged_csv       [meta, merged_quant.csv]      — the roster
                        //   contours         [patient_id, contours.json]
                        //   nucleus_contours [patient_id, json]            — empty unless compartments
                        //   phenotypes       [meta, phenotypes.csv]        — empty unless a panel
                        //   phenotype_qc     [meta, phenotype_qc.json]     — empty unless a panel
                        //   model_config     [patient_id, model.json]      — empty unless a panel
                        //   pyramid_channels [meta, [per-marker tiffs]]
                        //   cell_mask        [patient_id, cell_mask]
                        //   nuclei_mask      [patient_id, nuclei_mask]
    compartment_mode    // ParamUtils.compartmentMode(params) — resolved once by
                        // workflows/mirage.nf, threaded through postprocess.nf /
                        // add_cycle.nf unchanged. `.compartments` gates the nucleus
                        // contours; `.embedMasks` gates the second OME series.
    pheno_enabled       // Boolean — whether a panel was configured for THIS run, i.e.
                        // whether the three phenotype fields carry anything. Resolved by
                        // the caller (postprocess.nf reads params.panel_spec /
                        // params.panel_model once; add_cycle.nf has no PHENOTYPE stage
                        // and passes false) rather than re-read here, the same seam
                        // --registration_method and compartment_mode already have.

    main:

    // ========================================================================
    // EXPORT - QuPath-compatible GeoJSON + raw measurement CSV
    // ========================================================================
    // `contours` is declared BEFORE the four fields that fall back to it: an
    // orElseField may only name a field already bound, or the fallback would resolve
    // to null without a word.
    ch_export = PatientArtifacts.bundle(
        name    : 'ASSEMBLE_EXPORT: the per-patient EXPORT_GEOJSON tuple',
        metaFrom: 'merged_csv',
        fields  : [
            merged_csv      : ch_artifacts.merged_csv,
            contours        : ch_artifacts.contours,
            // EXTRACT_NUCLEI_PROPERTIES does not run at all without
            // --quantify_compartments, so this is a RUN-LEVEL gate, not a per-patient
            // absence: with compartments on, a patient missing its nucleus contours is
            // a dropped task and must still abort.
            nucleus_contours: [channel: ch_artifacts.nucleus_contours,
                               when: compartment_mode.compartments, orElseField: 'contours'],
            // The three phenotype slots. EXPORT_GEOJSON's own arg guard
            // (params.panel_spec || params.panel_model) suppresses the arguments when no
            // panel is configured, so the fallback file is staged and never read — which
            // is why re-using the contours file is harmless rather than merely cheap.
            phenotypes      : [channel: ch_artifacts.phenotypes,
                               when: pheno_enabled, orElseField: 'contours'],
            model_config    : [channel: ch_artifacts.model_config,
                               when: pheno_enabled, orElseField: 'contours'],
            phenotype_qc    : [channel: ch_artifacts.phenotype_qc,
                               when: pheno_enabled, orElseField: 'contours'],
        ],
    )

    // Named -> positional happens HERE, one line from the process it feeds, because a
    // process input tuple is positional and nothing can change that. What the bundle
    // removes is the forty lines that used to sit between the join chain and its
    // destructuring.
    EXPORT_GEOJSON(
        ch_export.map { b ->
            [b.meta, b.merged_csv, b.contours, b.nucleus_contours,
             b.phenotypes, b.model_config, b.phenotype_qc]
        }
    )

    // ========================================================================
    // PYRAMID - merge the per-patient channel TIFFs
    // ========================================================================
    // Merge intensity channels, and optionally embed cell + nuclei segmentation
    // masks as a SECOND, single-resolution uint32 OME series (Image:1). The
    // masks are never mixed into the intensity series itself: a >65,535-cell
    // uint32 label mask would force the whole intensity OME-TIFF to uint32,
    // which Bio-Formats/QuPath cannot read as a normal multi-channel image.
    // Cell objects are always delivered separately via cells.geojson; this
    // second series is an optional, additional way to carry the raw masks.
    // ONE condition. The gate used to also require quantify_compartments and
    // "Mean and Sum both requested", and neither was a real dependency: SEGMENT
    // emits cell_mask and nuclei_mask UNCONDITIONALLY (subworkflows/local/
    // segmentation.nf -- only nucleus_contours is gated on compartments), and
    // which statistics were computed has nothing to do with a raw mask series.
    //
    // Those extra conditions were the reason ParamUtils.validateCompartmentQuant
    // existed: --embed_masks true with either sibling off silently published a
    // pyramid with NO mask series, and the run only failed months later when its
    // --outdir was handed to add_cycle and EXTRACT_MASK_SERIES found no Image:1.
    // With one condition that footgun cannot be built, so the validator was
    // deleted rather than re-pointed -- a guard is worse than a design that makes
    // the mistake unrepresentable.
    def emit_masks = compartment_mode.embedMasks
    ch_pyramid_in = emit_masks
        ? PatientArtifacts.bundle(
                name    : 'ASSEMBLE_EXPORT: the per-patient MERGE_AND_PYRAMID tuple',
                metaFrom: 'pyramid_channels',
                fields  : [
                    pyramid_channels: ch_artifacts.pyramid_channels,
                    cell_mask       : ch_artifacts.cell_mask,
                    nuclei_mask     : ch_artifacts.nuclei_mask,
                ],
            )
            .map { b -> [b.meta, b.pyramid_channels, [b.cell_mask, b.nuclei_mask]] }
        : ch_artifacts.pyramid_channels.map { meta, tiffs -> [meta, tiffs, []] }

    // MERGE_AND_PYRAMID combines merge + pyramid generation in one step
    // This preserves OME-XML metadata (channel names, colors, pixel sizes)
    // and generates QuPath-compatible pyramidal OME-TIFF directly
    MERGE_AND_PYRAMID(ch_pyramid_in)

    emit:
    geojson           = EXPORT_GEOJSON.out.geojson
    geojson_wholecell = EXPORT_GEOJSON.out.geojson_wholecell
    csv               = EXPORT_GEOJSON.out.csv
    pyramid           = MERGE_AND_PYRAMID.out.pyramid
    size_logs         = EXPORT_GEOJSON.out.size_log.mix(MERGE_AND_PYRAMID.out.size_log)
    // `.first()` is applied HERE, inside the subworkflow, matching seg_qc.nf:112 and
    // adapters/valis_adapter.nf:151 — NOT the call-site style postprocess.nf uses for
    // its own inline processes. registration.nf:309 documents that asymmetry; every new
    // subworkflow de-duplicates its own versions so callers never have to know which
    // convention a given emit follows.
    versions          = EXPORT_GEOJSON.out.versions.first()
        .mix(MERGE_AND_PYRAMID.out.versions.first())
}
