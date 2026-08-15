/*
========================================================================================
    SUBWORKFLOW: REGISTER_PATIENT
========================================================================================
    Takes patient-grouped slides — [patient_id, reference_item, all_items] — and
    returns them registered, checkpointed, and with everything the QC stages need.

    WHY THIS EXISTS. There were two implementations of this. The linear path went
    through REGISTRATION; subworkflows/local/add_cycle.nf called VALIS_ADAPTER
    directly and re-did the surrounding wiring by hand. The copy had already lost,
    by construction, three things the original has:

      - the checkpoint manifest (an add_cycle run wrote none, so its --outdir could
        never be a second add_cycle's --prior_outdir),
      - the single-slide passthrough branch, and
      - the tiled/STARE backend.

    This file is now the only place that answers "how does a patient group become a
    registered stream?". Both callers assemble the group their own way — the linear
    path groups a preprocessed stream with a sized groupKey and resolves
    allow_auto_reference; add_cycle pairs each new slide with the FROZEN prior
    reference read out of --prior_outdir — and that difference is real, so grouping
    deliberately stays with the callers.

    Input:
        ch_grouped: [patient_id, reference_item, all_items]
                    reference_item = [meta, file]
                    all_items      = [[meta, file], ...]  (INCLUDING the reference)
        method:     registration backend name — 'tiled' selects TILED_ADAPTER (STARE),
                    anything else the classic VALIS_ADAPTER.

                    A plain String, not a channel: Nextflow binds workflow `take:`
                    values verbatim (same pattern as INPUT_CHECK's image_column /
                    auto_reference). It is an ARGUMENT rather than a read of
                    params.registration_method so that add_cycle keeps its current,
                    hard-wired VALIS behaviour exactly — workflows/mirage.nf rejects
                    --registration_method tiled in add_cycle mode, and until that
                    rejection is deliberately lifted this subworkflow must not be the
                    thing that quietly lifts it.

    Output:
        registered       [meta, file] — registered slides PLUS passthroughs, every one of
                                        them published into <pid>/registered/registered_slides/
                                        (see PUBLISH_PASSTHROUGH and Layout.registeredPath)
        images_multi     [meta, file] — the native (pre-registration) slides of
                                        multi-slide patients only; seg-QC input
        checkpoint_csv   the registration checkpoint manifest (see
                         REGISTERED_CHECKPOINT / Layout)
        transform          [patient_id, registrar.pickle | manifest] — seg-QC warper
        transform_by_slide [meta, manifest] — one per moving slide (empty under VALIS)
        stage_checkpoint   [patient_id, reg_stage_checkpoint/] (VALIS, reg_qc>=2 only)
        intrinsic_tre      the method's own TRE estimate (VALIS CSV / STARE JSON)
        size_logs        — the adapter's, unaggregated
        versions         — the adapter's, PLUS the checkpoint writer's

    Those last six are passed through from the adapter UNRENAMED. Both adapters emit the
    same vocabulary and fill any slot their method lacks with `Channel.empty()` (the
    contract is written out in either adapter's header), so this file holds no
    translation table and nothing below it has to know which backend ran.
========================================================================================
*/

include { VALIS_ADAPTER         } from './adapters/valis_adapter'
include { TILED_ADAPTER         } from './adapters/tiled_adapter'
include { REGISTERED_CHECKPOINT } from './registered_checkpoint'
include { PUBLISH_PASSTHROUGH   } from '../../modules/local/publish_passthrough'

workflow REGISTER_PATIENT {
    take:
    ch_grouped   // [patient_id, ref_item, all_items]
    method       // String: 'tiled' | 'valis'

    main:
    // Single-slide patients (only the reference, nothing to register) must NOT
    // be sent to the registration adapter: VALIS crashes on a lone image
    // ("negative dimensions are not allowed" / "M is None — no transformation
    // matrix"). For such a patient the reference IS the registered output, so we
    // branch it out here and pass it straight through to ch_registered below.
    ch_grouped_split = ch_grouped.branch { pid, ref, items ->
        single: items.size() == 1
        multi:  true
    }
    // EVERY SLIDE IN A PATIENT MUST DECLARE A DISTINCT PANEL, WHICHEVER BACKEND RUNS.
    //
    // This is the ONE place a repeated panel is judged, and it sits before the dispatch
    // below on purpose. It used to be judged twice, differently: VALIS_ADAPTER threw its
    // own message from inside its output-demux -- i.e. only after REGISTER had already
    // spent the compute -- while the tiled path never checked at all and SEG_QC's
    // `combine(by: [patient, signature])` cross-joined N slides against N transforms,
    // scoring every slide with every other slide's warp and writing identical filenames.
    // Same input, two outcomes. Checking on ch_grouped_multi means both adapters, both
    // callers (REGISTRATION and ADD_CYCLE) and any third backend get the same answer
    // without declaring anything.
    //
    // Note this runs on POST-preprocessing metas, which is the signature the adapters and
    // SEG_QC actually key on -- and that is the SAME channel SET the samplesheet declared.
    // preprocess.nf rebinds meta.channels to the list CONVERT_IMAGE wrote into
    // <prefix>_channels.txt, and that step MOVES the nuclear/fiducial channel to index 0
    // rather than removing it (bin/convert_image.py, via utils.metadata.nuclear_first).
    // PanelSignature sorts, so a permutation is one signature. The nuclear channel is not
    // dropped until SPLIT_CHANNELS, in POSTPROCESSING, long after this.
    //
    // Worth stating because an earlier version of this comment claimed the drop happened
    // in preprocessing, and concluded that two slides differing only in their nuclear
    // marker (DAPI|CD3|CD8 and CELLTOX|CD3|CD8) collapse here and are refused. They do not
    // collapse, and refusing them would abort a patient that used to complete. Both halves
    // of that are now pinned rather than reasoned about: the conversion permutes and
    // nothing else rebinds meta.channels (tests/test_panel_signature_survives_preprocess.py),
    // and that exact pair, built through preprocess.nf's own rebind expression, is accepted
    // (tests/lib_probe.nf).
    //
    // Why refusal rather than tolerance, given a repeat acquisition is legitimate science:
    // see lib/PanelSignature.groovy's header. Short version -- everything past
    // registration is keyed by MARKER NAME, so accepting the duplicate does not make the
    // repeat work, it just moves the collision somewhere quieter.
    ch_grouped_multi = ch_grouped_split.multi.map { pid, ref, items ->
        PanelSignature.requireUniqueWithinPatient(pid, items.collect { it[0] })
        [pid, ref, items]
    }
    // is_passthrough marks a slide that reaches the registered stream WITHOUT having
    // been registered. It is what the checkpoint writer keys on: nothing publishes an
    // unregistered slide into <pid>/registered/, so recording it there names a file
    // that does not exist (tests/checkpoint_manifest.nf.test).
    // TILED_ADAPTER sets the same flag on every patient's reference, which likewise
    // passes through unwarped.
    ch_passthrough   = ch_grouped_split.single.map { _pid, ref, _items ->
        [ref[0] + [is_passthrough: true], ref[1]]
    }  // [meta, file]

    // Flattened back to [meta, file] for consumers (seg-QC) that segment individual slides
    // rather than patient groups. Single-slide patients are excluded here on purpose: they
    // have no moving slide to score against, so segmenting their reference would compute a
    // full GPU StarDist WSI segmentation and discard it.
    //
    // NOTE: unlike the raw ch_preprocessed stream, `items` here comes out of the caller's
    // grouping closure, so under allow_auto_reference=true it carries that closure's
    // is_reference: true fill-in (registration.nf) for patients whose CSV marked no
    // reference. seg_qc.nf's reference/moving branch reads meta.is_reference, so this
    // channel is what lets an auto-reference multi-slide patient be scored at all — sourced
    // from the raw stream, that patient's reference branch was empty and it silently
    // produced zero seg-QC output.
    ch_images_multi = ch_grouped_multi.flatMap { pid, ref, items -> items }

    // ========================================================================
    // RUN REGISTRATION VIA METHOD-SPECIFIC ADAPTER
    // ========================================================================
    // The ENTIRE method dispatch. Both adapters emit the identical vocabulary
    // (registered / transform / transform_by_slide / stage_checkpoint / intrinsic_tre /
    // size_logs / versions), with Channel.empty() in any slot a method cannot fill — see
    // either adapter's header for the contract. So this binds ONE name, `ch_adapter`,
    // instead of a per-emit translation table plus the pre-declared empty channels that
    // used to exist only so the union of the two vocabularies could be assembled here.
    //
    // NOTE: the *old distributed-VALIS* low-memory path was archived 2026-07-24
    // (git tag archive/tiled-valis-2026-07-24). `method == 'tiled'` is a SEPARATE,
    // live STARE backend — don't confuse the two.
    if (method == 'tiled') {
        TILED_ADAPTER(ch_grouped_multi)
        ch_adapter = TILED_ADAPTER.out
    } else {
        VALIS_ADAPTER(ch_grouped_multi)
        ch_adapter = VALIS_ADAPTER.out
    }

    // Re-introduce single-slide patients (reference passed through unregistered)
    // into the registered stream for QC, checkpointing and postprocessing.
    //
    // EVERY PASSTHROUGH IS PUBLISHED AS A REGISTERED SLIDE, whichever adapter ran and
    // whatever produced it. Two kinds arrive here: this file's single-slide branch above,
    // and TILED_ADAPTER's reference (mixed into ch_adapter.registered already stamped
    // is_passthrough). They used to be recorded in csv/registered.csv at the path their
    // ORIGINAL producer published them to -- <pid>/preprocessed/ -- while warped slides
    // were recorded under <pid>/registered/registered_slides/. Since only the tiled
    // adapter passes a multi-slide patient's reference through, the tree a slide landed
    // in was a function of --registration_method: same logical slide, two layouts, and
    // every reader of the manifest had to know which backend wrote it.
    //
    // Routing them here rather than inside the adapter is what makes the fix
    // backend-independent: a third adapter that passes some slide through gets the same
    // treatment without doing anything, because the only thing it has to declare is
    // is_passthrough (which it must set anyway -- see the branch's comment above).
    ch_all = ch_adapter.registered.mix(ch_passthrough)
    ch_by_provenance = ch_all.branch { meta, _file ->
        passthrough: meta.is_passthrough
        warped:      true
    }
    PUBLISH_PASSTHROUGH(ch_by_provenance.passthrough)
    ch_registered = ch_by_provenance.warped.mix(PUBLISH_PASSTHROUGH.out.registered)

    // ========================================================================
    // CHECKPOINT
    // ========================================================================
    // How many manifest rows each patient contributes: one per slide in the group,
    // passthrough included. Taken from the GROUP rather than from meta.images_count
    // because those two disagree on the add_cycle path — see REGISTERED_CHECKPOINT's
    // header. This is the only place both callers' groups are visible, which is why
    // the count is derived here rather than inside the writer.
    ch_expected_rows = ch_grouped.map { pid, _ref, items -> [pid, items.size()] }

    REGISTERED_CHECKPOINT(ch_registered, ch_expected_rows)

    emit:
    registered         = ch_registered
    images_multi       = ch_images_multi
    checkpoint_csv     = REGISTERED_CHECKPOINT.out.csv
    // Straight through from the adapter, under the adapter's own names — see the header.
    transform          = ch_adapter.transform
    transform_by_slide = ch_adapter.transform_by_slide
    stage_checkpoint   = ch_adapter.stage_checkpoint
    intrinsic_tre      = ch_adapter.intrinsic_tre
    size_logs          = ch_adapter.size_logs
    // The checkpoint writer's versions row is mixed in HERE, not left dangling on
    // REGISTERED_CHECKPOINT.out. Every other caller of CHECKPOINT_WRITER
    // (preprocess/segmentation/postprocess) mixes it into its own versions stream; this
    // path did not, so a `--start registration --stop registration` run -- the only run
    // in which this is the sole CHECKPOINT_WRITER instance -- published a QC report
    // missing the row.
    versions           = ch_adapter.versions
        .mix(REGISTERED_CHECKPOINT.out.versions)
        .mix(PUBLISH_PASSTHROUGH.out.versions.first())
}
