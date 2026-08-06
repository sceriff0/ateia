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

    Caller differences are parameterised through `take:`, never by branching on
    params.mode in here:
      - `ch_nuc_contours`  — real nucleus contours under quantify_compartments, or the
                             cell contours as a harmless placeholder.
      - `ch_pheno_extras`  — [patient_id, phenotypes, model_config]. POSTPROCESSING
                             supplies the real PHENOTYPE/COMPILE_PANEL products when a
                             panel is configured; otherwise (and always in add_cycle,
                             which has no PHENOTYPE stage) the caller reuses the
                             contours file for both slots. EXPORT_GEOJSON's own arg
                             guard (params.panel_spec || params.panel_model) suppresses
                             the arguments, so the placeholder is never read.
      - `ch_pyramid_masks` — [patient_id, cell_mask, nuclei_mask]: freshly segmented in
                             POSTPROCESSING, reused from the prior run in add_cycle.

    One deliberate normalisation: the embed_masks gate below joins ch_pyramid_masks with
    `join(by: 0)`, which is what POSTPROCESSING's copy did; add_cycle's copy used
    `combine(by: 0)`. Both channels carry exactly one entry per patient, so at the
    reachable cardinality the two operators are equivalent — but join is the stricter of
    the pair (a duplicate mask entry would fan out under combine and error under join),
    so the shared version keeps join.
========================================================================================
*/

include { EXPORT_GEOJSON    } from '../../modules/local/export_geojson'
include { MERGE_AND_PYRAMID } from '../../modules/local/merge_and_pyramid'

workflow ASSEMBLE_EXPORT {
    take:
    ch_merged_csv       // [meta, merged_quant.csv]
    ch_contours         // [patient_id, contours.json]
    ch_nuc_contours     // [patient_id, nucleus_contours.json | contours.json placeholder]
    ch_pheno_extras     // [patient_id, phenotypes, model_config]  (placeholders allowed)
    ch_pyramid_channels // [meta, [per-marker tiffs]]  — one entry per patient
    ch_pyramid_masks    // [patient_id, cell_mask, nuclei_mask]

    main:

    // ========================================================================
    // EXPORT - QuPath-compatible GeoJSON + raw measurement CSV
    // ========================================================================
    ch_for_export = ch_merged_csv
        .map { meta, csv -> [meta.patient_id, meta, csv] }
        .join(ch_contours, by: 0)
        .join(ch_nuc_contours, by: 0)
        .join(ch_pheno_extras, by: 0)
        .map { _pid, meta, csv, contours_json, nuc_json, phenotypes, model_config ->
            [meta, csv, contours_json, nuc_json, phenotypes, model_config]
        }

    EXPORT_GEOJSON(ch_for_export)

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
    def emit_masks = params.embed_masks && params.quantify_compartments && params.expanded_quantification
    ch_pyramid_in = emit_masks
        ? ch_pyramid_channels
            .map { meta, tiffs -> [meta.patient_id, meta, tiffs] }
            .join(ch_pyramid_masks, by: 0)
            .map { _pid, meta, tiffs, cell_mask, nuclei_mask -> [meta, tiffs, [cell_mask, nuclei_mask]] }
        : ch_pyramid_channels.map { meta, tiffs -> [meta, tiffs, []] }

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
    // adapters/valis_adapter.nf:151 — NOT the call-site style postprocess.nf:420-442
    // uses for its own inline processes. registration.nf:309 documents that
    // asymmetry; every new subworkflow de-duplicates its own versions so callers
    // never have to know which convention a given emit follows.
    versions          = EXPORT_GEOJSON.out.versions.first()
        .mix(MERGE_AND_PYRAMID.out.versions.first())
}
