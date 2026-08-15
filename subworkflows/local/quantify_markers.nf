/*
========================================================================================
    SUBWORKFLOW: QUANTIFY_MARKERS
========================================================================================
    The quantification chain, shared by the linear postprocessing path and the
    incremental add_cycle path:

        split-channel stacks -> per-marker meta -> per-patient group -> QUANTIFY

    The group used to sit on the FAR side of QUANTIFY, regathering per-marker CSVs
    that a per-marker fan-out had just produced. It is the same gather of the same
    items, one process earlier, and moving it is what makes QUANTIFY one task per
    patient instead of one per (patient x marker) — 204 tasks down to 12 on the
    reference panel, each of which used to re-stage and re-read the same whole-cell
    mask. bin/quantify.py's load->compute->discard loop is what keeps that from
    multiplying peak memory by the marker count; see its module docstring.

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
// single per-patient list feeding ASSEMBLE_EXPORT/MERGE_AND_PYRAMID. The grouping
// mechanics -- sized groupKey, remainder:true, GroupKey unwrap, canonical order --
// are lib/PatientGroup.groovy's; see its header. What is local here is the SIZE's
// consequence: an under-count on this particular grouping is the one failure mode
// that ABORTS the run outright rather than degrading, because a short channel list
// trips MERGE_AND_PYRAMID's memory closure (conf/modules.config:330-337) with
// "No such file or directory: channels". Over-counting is the safe direction.
//
// ch_tagged: [patient_id, channels_count, tiff] -- one entry per patient+marker,
// ALREADY deduplicated by [patient_id, marker] by the caller (postprocess.nf keeps
// the first occurrence of a repeated marker name; add_cycle keeps whichever cycle's
// tiff should win a new-vs-prior collision). The two scalars are re-wrapped into a
// meta here because that is the shape every grouping in this pipeline speaks; the
// callers keep the flat tuple because neither of them has a patient-level meta at
// that point (add_cycle's count is summed across two channel streams).
def groupTiffsByPatient(ch_tagged) {
    return PatientGroup.byPatient(
            ch_tagged.map { patient_id, channels_count, tiff ->
                [[patient_id: patient_id, channels_count: channels_count], tiff]
            },
            name  : 'QUANTIFY_MARKERS: the per-patient channel tiffs feeding MERGE_AND_PYRAMID',
            size  : 'channels_count',
            sortBy: { _meta, tiff -> tiff.name },
        )
        .map { patient_id, pairs ->
            def patient_meta = [
                id: patient_id,
                patient_id: patient_id,
                is_reference: false  // Not relevant at patient level
            ]
            [patient_meta, pairs.collect { pair -> pair[1] }]
        }
}


workflow QUANTIFY_MARKERS {
    take:
    ch_split_channels   // [meta, tiffs]                        — SPLIT_CHANNELS.out.channels
    ch_masks            // [patient_id, cell_mask, nuclei_mask]
    compartment_mode    // ParamUtils.compartmentMode(params) — resolved ONCE by
                        // workflows/mirage.nf and threaded down, the same seam
                        // --registration_method has. `.statistics` is read here to
                        // decide whether REDSEA runs; this subworkflow must NOT read
                        // params.quantify_statistics itself
                        // (tests/test_compartment_mode_routing.py enforces that).

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
            //
            // A marker has TWO names and only one of them is its identity.
            // `channel_name` is the DECLARED name -- the samplesheet's spelling,
            // recovered from meta.channels by lib/ChannelName.groovy -- because it
            // fills the <marker> slot of the "<marker>: <Compartment>: <Statistic>"
            // key that qupath-extension-flowpath parses case-sensitively (G5).
            // `channel_stem` is the sanitised filename form. It names the
            // per-marker CSV -- the grouping below builds
            // `<patient>_<stem>_quant.csv` from it -- and nothing else. (`id` is
            // built from `tiff.baseName` directly, one line down; an earlier
            // version of this comment claimed `id` read `channel_stem`, which it
            // never did.) The two forms are deliberately BOTH carried: the
            // declared name cannot go in a filename and the stem cannot go in a
            // key, so neither can be re-derived from the other at the point of
            // use without reintroducing exactly this defect.
            //
            // This used to be `channel_name: tiff.baseName`: identity read back OFF
            // DISK, already mangled by the filename allowlist. A panel declaring
            // 'HLA.DR' published 'HLA_DR: Cell: Median' -- a key its own consumer
            // never looks for, since bin/phenotype_cells.py builds the lookup from
            // the DECLARED name and, on a miss, degrades to an all-zero column in
            // silence.
            tiff_list.collect { tiff ->
                def channel_meta = meta + [
                    id: "${meta.patient_id}_${tiff.baseName}",
                    channel_stem: tiff.baseName,
                    channel_name: ChannelName.declaredFor(tiff.baseName, meta.channels)
                ]
                [channel_meta, tiff]
            }
        }
    ch_flatmapped = viewIfDebug(
        ch_flatmapped,
        { meta, tiff -> "After flatMap: id=${meta.id}, channel=${meta.channel_name}, tiff=${tiff.name}" }
    )

    // ========================================================================
    // GROUP - gather each patient's markers BEFORE quantifying them, so QUANTIFY
    // runs ONCE PER PATIENT rather than once per (patient x marker).
    //
    // This gather used to sit on the far side of QUANTIFY, regrouping the
    // per-marker CSVs. It is the same gather of the same items, one process
    // earlier, and moving it is what removes the fan-out: 12 patients x 17
    // markers was 204 tasks, each re-staging and re-reading the same whole-cell
    // mask and each asking for a flat 128 GB with executor.queueSize = 20.
    //
    // Deduplicated by [patient_id, marker], first occurrence winning, and that
    // now happens BEFORE quantification instead of after it: the repeat used to
    // be quantified anyway and its CSV then discarded. It is also load-bearing
    // rather than merely thrifty here — QUANTIFY stages the whole list into one
    // work directory, and bin/split_multichannel.py names its outputs by
    // sanitised channel name alone ('PANCK.tiff'), so the same marker arriving
    // from two of a patient's slides would be an input filename collision.
    //
    // The grouping mechanics are lib/PatientGroup.groovy's. What is local is the
    // provenance of the size: channels_count is EXACT for both callers, because
    // CsvUtils.countChannelsPerPatient applies MarkerUtils.splitOutputChannels --
    // the same rule SPLIT_CHANNELS applies -- so it counts the markers that
    // actually reach QUANTIFY rather than the channels the samplesheet declares.
    // (It used to union declared channels with no reference awareness, which
    // over-counted a reference-less add_cycle sheet by exactly its dropped nuclear
    // channel, and the group could then never fill.)
    //
    // PatientGroup applies `remainder: true` unconditionally, which is what keeps
    // an OVER-count safe: the surplus slot never fills at its too-large target
    // size, but the group is still emitted -- complete, just late, at channel
    // close. An UNDER-count is NOT symmetric: it makes groupTuple emit the patient
    // TWICE (once at the too-small target size, once as the remainder leftover),
    // which is now TWO QUANTIFY tasks for one patient, each writing a disjoint
    // subset of the markers, and what happens to those two emissions downstream
    // depends entirely on the CALLER:
    //   - add_cycle.nf's `.combine(by: 0)` against the prior base table is a true
    //     cross-product: MERGE_QUANT_CSVS runs TWICE, both invocations writing the
    //     SAME <pid>/quantification/merged_quant.csv.
    //   - postprocess.nf's `.join(ch_morphology, by: 0)` is a keyed inner join: the
    //     surplus group is silently DISCARDED, so merged_quant.csv loses markers
    //     with no error at all.
    //   - the pyramid grouping above is worse still -- see its comment.
    // Over-counting is the safe direction, which is why countChannelsPerPatient
    // errs high whenever it cannot be certain.
    // ========================================================================
    ch_patient_panel = PatientGroup.byPatient(
            ch_flatmapped
                .map { meta, tiff -> [[meta.patient_id, meta.channel_name], meta, tiff] }
                .unique { entry -> entry[0] }
                .map { _key, meta, tiff -> [meta, tiff] },
            name  : 'QUANTIFY_MARKERS: the per-patient marker tiffs feeding QUANTIFY',
            size  : 'channels_count',
            sortBy: { meta, _tiff -> meta.channel_name },
        )
        .map { patient_id, pairs ->
            // `metas[0]` is not incidental here: the channels/is_reference/
            // channel_name it carries travel into QUANTIFY, MERGE_QUANT_CSVS,
            // EXPORT_GEOJSON and EXPORT_SPATIALDATA. Under arrival order two
            // identical runs picked different ones. PatientGroup's canonical order
            // makes it a function of the data (the alphabetically first marker).
            //
            // `+` creates a new top-level map, but Groovy's Map.plus() is
            // cloneSimilarMap(left).putAll(right) -- clone-then-putAll, operationally
            // identical to clone(). meta.channels is still the same List reference as
            // in the original meta. See subworkflows/local/adapters/valis_adapter.nf:82-88
            // for why that matters and why toSorted() (not sort()) is mandatory
            // wherever meta.channels is read.
            def meta = pairs[0][0] + [id: patient_id]
            // The per-marker DECLARED name and the per-marker CSV filename, paired,
            // one entry per tiff and in the same order as the tiffs. They travel as
            // ONE map rather than two parallel lists so the pair cannot come apart;
            // modules/local/quantify.nf renders both, and bin/quantify.py refuses a
            // length mismatch against the tiffs rather than letting zip() truncate.
            // The filename is `<patient>_<stem>_quant.csv` -- exactly what the
            // per-marker fan-out published, since meta.id was `<patient>_<stem>`.
            def markers = pairs.collect { pair ->
                [name  : pair[0].channel_name,
                 output: "${patient_id}_${pair[0].channel_stem}_quant.csv".toString()]
            }
            [patient_id, meta, markers, pairs.collect { pair -> pair[1] }]
        }
    ch_patient_panel = viewIfDebug(
        ch_patient_panel,
        { patient_id, _meta, markers, _tiffs ->
            "Grouped panel: key=${patient_id}, markers=${markers*.name}"
        }
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
    // REDSEA runs iff the caller's resolved statistic list names it. There is no
    // `params.redsea` boolean -- membership of the list IS the switch. Neither
    // caller (postprocess.nf, add_cycle.nf) knows REDSEA exists, and both get it,
    // because both already hand this subworkflow the masks it needs.
    //
    // REDSEA (Bai et al., Front. Immunol. 2021;12:652631) splits into a
    // mask-only part and a channel-only part; that split is the whole reason it
    // fits here rather than as a new stage. The mask part is one pass per
    // patient (REDSEA_MATRIX); the channel part is a sparse mat-vec that rides
    // along inside QUANTIFY's per-marker loop below, so turning REDSEA on adds no
    // serialisation and no extra fan-out. The geometry is now loaded ONCE per
    // patient task rather than once per opted-in marker task, and shared read-only
    // across the loop (bin/quantify.py's _RedseaGeometry).
    //
    // The geometry is a REQUIRED input of QUANTIFY, not an optional one. A
    // conditional input arity would make QUANTIFY two different processes
    // depending on a param -- so when REDSEA is off every task is handed
    // assets/NO_REDSEA and modules/local/quantify.nf tests for that name. The
    // placeholder is a few bytes and stages once per task.
    def redsea_enabled = compartment_mode.statistics.contains('REDSEA')
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

    // combine(by: 0), not join: an UNDER-declared channels_count emits a patient
    // twice (see the GROUP comment), and join would drop the second. The masks are
    // now staged ONCE per patient rather than once per marker — the fan-out used to
    // re-stage and re-read the same whole-cell mask for all 17 of them.
    ch_for_quant = ch_patient_panel
        .combine(ch_masks_viewed, by: 0)
        .combine(ch_redsea, by: 0)
        .map { _patient_id, meta, markers, tiffs, cell_mask, nuclei_mask, redsea_npz ->
            [meta, markers, tiffs, cell_mask, nuclei_mask, redsea_npz]
        }
    ch_for_quant = viewIfDebug(
        ch_for_quant,
        { meta, markers, _tiffs, _cell, _nuc, _rs ->
            "After combine: patient=${meta.patient_id}, markers=${markers*.name}"
        }
    )

    QUANTIFY(ch_for_quant)
    // QUANTIFY already emits [patient meta, [per-marker CSVs]] -- the gather that
    // used to stand here happened before it. All that is left is to pin the ORDER
    // of the collected glob: Nextflow hashes list inputs POSITIONALLY, so
    // MERGE_QUANT_CSVS' task hash (and its `--csv-files` argument order) must be a
    // function of the data rather than of directory-listing order, or -resume
    // misses and cascades. Sorting by NAME is safe here in a way
    // groupTuple(sort:) never was: there is only ONE list, so nothing can be
    // re-paired with the wrong meta.
    ch_grouped_csvs = QUANTIFY.out.individual_csv
        .map { meta, csvs ->
            [meta, (csvs instanceof List ? csvs : [csvs]).toSorted { it.name }]
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
