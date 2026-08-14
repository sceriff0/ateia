/*
========================================================================================
    SUBWORKFLOW: QUANTIFY_MARKERS
========================================================================================
    The per-marker quantification chain, shared by the linear postprocessing path
    and the incremental add_cycle path:

        split-channel stacks -> per-marker fan-out -> QUANTIFY -> per-patient group

    Both callers used to carry their own copy of this chain and the copies drifted:
    add_cycle grouped the quantification CSVs with a bare `.groupTuple()` and so lost
    the `groupKey(patient_id, channels_count)` streaming hint that the postprocessing
    copy had. There is now exactly ONE grouping implementation and it carries the hint.

    The two callers differ only in where the masks come from (SEGMENT vs the prior
    run's reused masks) and in what they do with the grouped CSVs afterwards
    (join morphology.csv vs merge onto a prior base table). Both differences are
    parameterised through `take:`/`emit:` — nothing in here branches on params.mode.

    This file also exports `groupTiffsByPatient`, a plain function (not a process
    or workflow — Nextflow's `include` can pull in either) used by both callers'
    OWN pyramid-channel grouping, the same drift-prone shape as the CSV grouping
    above. See its own doc comment for why it lives here rather than in
    postprocess.nf or assemble_export.nf.
========================================================================================
*/

include { QUANTIFY } from '../../modules/local/quantify'
include { REDSEA_MATRIX } from '../../modules/local/redsea_matrix'

// The pipeline's only --debug_channels view helper. It used to be duplicated in
// postprocess.nf; that copy went with the chain its `.view()` calls annotate, so there
// is one implementation and one caller file. Off unless --debug_channels, log-only.
def viewIfDebug(channel, Closure formatter) {
    return params.debug_channels ? channel.view(formatter) : channel
}

// The pyramid-channel grouping shared by postprocess.nf's ch_split_grouped and
// add_cycle.nf's ch_all_channels. Both group one-tiff-per-marker entries into a
// single per-patient list feeding ASSEMBLE_EXPORT/MERGE_AND_PYRAMID, and both need
// the EXACT SAME channels_count-sized groupKey + remainder:true streaming hint
// this file's CSV grouping above uses, for the same reason: an under-count here is
// the worse of the two grouping's failure modes (see the GROUP comment above) — it
// trips MERGE_AND_PYRAMID's memory closure (conf/modules.config:330-337) and
// ABORTS the run outright, rather than silently degrading. add_cycle's own copy of
// this grouping used to be a bare `.groupTuple()` with no size hint at all — this
// is the fix, and the reason it now lives in exactly one place.
//
// ch_tagged: [patient_id, channels_count, tiff] — one entry per patient+marker,
// ALREADY deduplicated by [patient_id, marker] by the caller (postprocess.nf keeps
// the first occurrence of a repeated marker name; add_cycle keeps whichever cycle's
// tiff should win a new-vs-prior collision). channels_count may be null, in which
// case grouping falls back to a bare key — correct, just non-streaming.
def groupTiffsByPatient(ch_tagged) {
    return ch_tagged
        .map { patient_id, channels_count, tiff ->
            def gkey = channels_count
                ? groupKey(patient_id, channels_count)
                : patient_id
            [gkey, tiff]
        }
        .groupTuple(by: 0, remainder: true)
        .map { patient_id, tiffs_unordered ->
            // CANONICAL ORDER. groupTuple emits in ARRIVAL order, and this list becomes
            // MERGE_AND_PYRAMID's `path` input, which Nextflow hashes POSITIONALLY -- so
            // an identical rerun produced a different task hash and -resume missed.
            // Sorting by name makes the order a function of the data. Safe to sort in
            // isolation here (unlike the metas+csvs grouping below) because this group
            // carries exactly ONE list, so there is no pairing to break.
            def tiffs = tiffs_unordered.toSorted { it.name }
            // Extract actual patient_id from groupKey wrapper if needed
            def pid = patient_id.toString()
            def patient_meta = [
                id: pid,
                patient_id: pid,
                is_reference: false  // Not relevant at patient level
            ]
            [patient_meta, tiffs]
        }
}

workflow QUANTIFY_MARKERS {
    take:
    ch_split_channels   // [meta, tiffs]                        — SPLIT_CHANNELS.out.channels
    ch_masks            // [patient_id, cell_mask, nuclei_mask]

    main:

    ch_split_viewed = viewIfDebug(
        ch_split_channels,
        { meta, tiffs -> "SPLIT_CHANNELS output: patient=${meta.patient_id}, tiffs=${tiffs*.name}" }
    )

    ch_flatmapped = ch_split_viewed
        .flatMap { meta, tiffs ->
            // Ensure tiffs is always a list (handle both single file and multiple files)
            def tiff_list = tiffs instanceof List ? tiffs : [tiffs]

            // Create unique meta map for each channel file. `+` creates a new
            // top-level map, but Groovy's Map.plus() is
            // cloneSimilarMap(left).putAll(right) -- clone-then-putAll,
            // operationally identical to clone(). meta.channels is still the
            // same List reference as in the original meta. See
            // subworkflows/local/adapters/valis_adapter.nf:82-88 for why
            // that matters and why toSorted() (not sort()) is mandatory
            // wherever meta.channels is read.
            tiff_list.collect { tiff ->
                def channel_meta = meta + [
                    id: "${meta.patient_id}_${tiff.baseName}",
                    channel_name: tiff.baseName
                ]
                [channel_meta, tiff]
            }
        }
    ch_flatmapped = viewIfDebug(
        ch_flatmapped,
        { meta, tiff -> "After flatMap: id=${meta.id}, channel=${meta.channel_name}, tiff=${tiff.name}" }
    )

    ch_for_combine = ch_flatmapped
        .map { meta, tiff -> [meta.patient_id, meta, tiff] }
    ch_for_combine = viewIfDebug(
        ch_for_combine,
        { patient_id, _meta, _tiff -> "Before combine: key=${patient_id}, channel=${_meta.channel_name}" }
    )

    // Both masks (cell + nuclei) arrive keyed by patient_id. QUANTIFY only reads the
    // nuclear one when params.quantify_compartments is set (per-compartment signal).
    ch_masks_viewed = viewIfDebug(
        ch_masks,
        { patient_id, _cell, _nuc -> "Masks available: key=${patient_id}, cell=${_cell.name}, nuclei=${_nuc.name}" }
    )

    // ========================================================================
    // REDSEA - lateral spillover compensation geometry (once per patient)
    // ========================================================================
    // `params.redsea` is read HERE and only here, the same seam
    // registration.nf gives --registration_method: one subworkflow owns the
    // decision and everything downstream takes the result as data. Neither
    // caller (postprocess.nf, add_cycle.nf) knows REDSEA exists, and both get it,
    // because both already hand this subworkflow the masks it needs.
    //
    // REDSEA (Bai et al., Front. Immunol. 2021;12:652631) splits into a
    // mask-only part and a channel-only part; that split is the whole reason it
    // fits here rather than as a new stage. The mask part is one pass per
    // patient (REDSEA_MATRIX); the channel part is a sparse mat-vec that rides
    // along inside the per-marker QUANTIFY fan-out below, so turning REDSEA on
    // adds no serialisation and no extra fan-out.
    //
    // The geometry is a REQUIRED input of QUANTIFY, not an optional one. A
    // conditional input arity would make QUANTIFY two different processes
    // depending on a param -- so when REDSEA is off every task is handed
    // assets/NO_REDSEA and modules/local/quantify.nf tests for that name. The
    // placeholder is a few bytes and stages once per task.
    def redsea_enabled = params.redsea
    ch_cell_mask_only = ch_masks_viewed.map { patient_id, cell_mask, _nuclei ->
        [[id: patient_id.toString(), patient_id: patient_id.toString()], cell_mask]
    }

    // REDSEA_MATRIX.out.qc is deliberately NOT re-emitted from this subworkflow.
    // It reaches the user through conf/modules.config's publishDir, at
    // <outdir>/<patient>/quantify/<patient>_redsea_qc.json. Routing it into
    // FINAL_QC's aggregated report would need a new ParamUtils.STEPS qcKind AND a
    // new positional input on GENERATE_QC_REPORT; an emit added now against that
    // future would be an unconsumed channel, which is the "keep it just in case"
    // this repo deletes rather than keeps.
    if (redsea_enabled) {
        REDSEA_MATRIX(ch_cell_mask_only)
        ch_redsea = REDSEA_MATRIX.out.geometry.map { meta, npz -> [meta.patient_id, npz] }
        ch_redsea_versions = REDSEA_MATRIX.out.versions.first()
        ch_redsea_sizes = REDSEA_MATRIX.out.size_log
    }
    else {
        // Null object, the same convention the registration adapters use for an
        // absent TRE: the downstream consumer tolerates the absence rather than
        // the producer being made conditional.
        ch_redsea = ch_cell_mask_only.map { meta, _cell ->
            [meta.patient_id, file("${projectDir}/assets/NO_REDSEA", checkIfExists: true)]
        }
        ch_redsea_versions = Channel.empty()
        ch_redsea_sizes = Channel.empty()
    }

    // combine(by: 0), not join: one mask pair fans out over the patient's N markers.
    ch_for_quant = ch_for_combine
        .combine(ch_masks_viewed, by: 0)
        .combine(ch_redsea, by: 0)
        .map { _patient_id, meta, tiff, cell_mask, nuclei_mask, redsea_npz ->
            [meta, tiff, cell_mask, nuclei_mask, redsea_npz]
        }
    ch_for_quant = viewIfDebug(
        ch_for_quant,
        { meta, _tiff, _cell, _nuc, _rs -> "After combine: patient=${meta.patient_id}, channel=${meta.channel_name}" }
    )

    QUANTIFY(ch_for_quant)

    // ========================================================================
    // GROUP - Collect per-marker CSVs by patient_id
    // Deduplicate by patient_id + marker (take first occurrence if same marker appears multiple times)
    // Use groupKey for streaming - emits as soon as channels_count items collected
    //
    // channels_count is EXACT for both callers: CsvUtils.countChannelsPerPatient applies
    // MarkerUtils.splitOutputChannels, the same rule SPLIT_CHANNELS applies, so it counts
    // the markers that actually reach QUANTIFY rather than the channels the samplesheet
    // declares. (It used to union declared channels with no reference awareness, which
    // over-counted a reference-less add_cycle sheet by exactly its dropped nuclear
    // channel — the group could then never fill.)
    //
    // `remainder: true` is kept anyway, as a safety net against a future miscount. An
    // OVER-count is safe either way, and strictly better than before this branch: the
    // surplus slot never fills at its (too-large) target size, but `remainder: true`
    // still emits it -- complete, just late, at channel close -- instead of the
    // pre-branch silent drop where an over-counted group never emitted at all. An
    // UNDER-count is NOT uniformly safe, and it is NOT "a per-patient
    // consumer runs twice" in general — an undercount makes groupTuple emit the patient
    // TWICE (once at the too-small target size, once as the `remainder: true` leftover),
    // and what happens to those two emissions depends entirely on how the CALLER
    // consumes this grouped_csv, which differs per caller:
    //   - add_cycle.nf's `.combine(by: 0)` against the prior base table (the "MERGE new
    //     marker CSVs onto the prior merged table" section) is a true cross-product:
    //     MERGE_QUANT_CSVS actually runs TWICE, both invocations writing the SAME
    //     <pid>/quantification/merged_quant.csv.
    //   - postprocess.nf's `.join(ch_morphology, by: 0)` feeding MERGE_QUANT_CSVS on the
    //     linear path is a keyed inner join: the surplus group is silently DISCARDED, so
    //     merged_quant.csv loses markers with no error at all.
    //   - postprocess.nf's OWN, separately-built grouping for the pyramid path
    //     (ch_split_grouped, same channels_count, same remainder:true — see its comment)
    //     is worse still: a one-file surplus group trips the MERGE_AND_PYRAMID memory
    //     closure (conf/modules.config:330-337 — pre-existing, NOT fixed here) and the
    //     run ABORTS with "No such file or directory: channels".
    // Over-counting is still the safe direction, which is why countChannelsPerPatient
    // errs high whenever it cannot be certain.
    // ========================================================================
    ch_grouped_csvs = QUANTIFY.out.individual_csv
        .map { meta, csv ->
            def marker = meta.channel_name  // Extract marker name
            [[meta.patient_id, marker], meta, csv]  // Key by [patient_id, marker]
        }
        .unique { entry -> entry[0] }  // Keep only first occurrence of each [patient_id, marker] pair
        .map { key, meta, csv ->
            // Use groupKey for streaming if channels_count is available
            def gkey = meta.channels_count
                ? groupKey(key[0], meta.channels_count)
                : key[0]
            [gkey, meta, csv]
        }
        .groupTuple(by: 0, remainder: true)
        .map { patient_id, metas_unordered, csvs_unordered ->
            // CANONICAL ORDER, and it is not only a caching concern here. groupTuple
            // emits in ARRIVAL order, so BOTH the csv list reaching MERGE_QUANT_CSVS
            // (hashed positionally -> -resume missed) AND `metas[0]` below were
            // whichever marker finished quantifying first. Two identical runs picked
            // different metas, so the channels/is_reference/channel_name carried into
            // MERGE_QUANT_CSVS, EXPORT_GEOJSON and EXPORT_SPATIALDATA differed run to
            // run:
            //     run A: [... is_reference:true,  channels:[DAPI, PANCK, SMA], channel_name:SMA]
            //     run B: [... is_reference:false, channels:[DAPI, CD3, CD8],   channel_name:CD3]
            //
            // Pair FIRST, then sort the pairs. NEVER `groupTuple(sort:)` -- it orders
            // each grouped list independently and silently re-pairs meta with the wrong
            // file (see registration.nf's grouping for the worked example).
            def paired = [metas_unordered, csvs_unordered].transpose()
                            .toSorted { it[0].channel_name }
            def metas = paired.collect { it[0] }
            def csvs  = paired.collect { it[1] }
            // `+` creates a new top-level map, but Groovy's Map.plus() is
            // cloneSimilarMap(left).putAll(right) -- clone-then-putAll,
            // operationally identical to clone(). metas[0].channels is still
            // the same List reference as in the original meta. See
            // subworkflows/local/adapters/valis_adapter.nf:82-88 for why
            // that matters and why toSorted() (not sort()) is mandatory
            // wherever meta.channels is read.
            // Extract actual patient_id from groupKey wrapper if needed
            def meta = metas[0] + [id: patient_id.toString()]
            [meta, csvs]
        }

    emit:
    // [meta, [per-marker quant csvs]] — one entry per patient.
    grouped_csv = ch_grouped_csvs
    size_logs   = QUANTIFY.out.size_log.mix(ch_redsea_sizes)
    // `.first()` is applied HERE, inside the subworkflow, matching seg_qc.nf:112 and
    // adapters/valis_adapter.nf:151 — NOT the call-site style postprocess.nf:420-442
    // uses for its own inline processes. registration.nf:309 documents that
    // asymmetry; every new subworkflow de-duplicates its own versions so callers
    // never have to know which convention a given emit follows.
    versions    = QUANTIFY.out.versions.first().mix(ch_redsea_versions)
}
