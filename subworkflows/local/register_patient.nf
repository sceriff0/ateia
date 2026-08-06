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
        registered       [meta, file] — registered slides PLUS passthroughs
        images_multi     [meta, file] — the native (pre-registration) slides of
                                        multi-slide patients only; seg-QC input
        checkpoint_csv   the registration checkpoint manifest (see
                         REGISTERED_CHECKPOINT / Layout)
        transform          [patient_id, registrar.pickle | manifest] — seg-QC warper
        transform_by_slide [meta, manifest] — one per moving slide (empty under VALIS)
        stage_checkpoint   [patient_id, reg_stage_checkpoint/] (VALIS, reg_qc>=2 only)
        intrinsic_tre      the method's own TRE estimate (VALIS CSV / STARE JSON)
        size_logs / versions — the adapter's, unaggregated

    Those last six are passed through from the adapter UNRENAMED. Both adapters emit the
    same vocabulary and fill any slot their method lacks with `Channel.empty()` (the
    contract is written out in either adapter's header), so this file holds no
    translation table and nothing below it has to know which backend ran.
========================================================================================
*/

include { VALIS_ADAPTER         } from './adapters/valis_adapter'
include { TILED_ADAPTER         } from './adapters/tiled_adapter'
include { REGISTERED_CHECKPOINT } from './registered_checkpoint'

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
    ch_grouped_multi = ch_grouped_split.multi
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
    ch_registered = ch_adapter.registered.mix(ch_passthrough)

    // ========================================================================
    // CHECKPOINT
    // ========================================================================
    REGISTERED_CHECKPOINT(ch_registered)

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
    versions           = ch_adapter.versions
}
