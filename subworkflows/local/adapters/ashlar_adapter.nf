/*
========================================================================================
    ASHLAR_ADAPTER — the ASHLAR registration backend (benchmark baseline)
========================================================================================
    ASHLAR (labsyspharm) is the field-standard cyclic-IF registrar. It is wired in here as a
    THIRD backend so the real-sample arm benchmark can rank it against VALIS and STARE on the
    same reg_qc=2 segmentation-overlap metric, in the same figure — not as a parallel table in
    a different metric family, which is what an out-of-band ORB harness produces.

    THREE THINGS MAKE THAT POSSIBLE, AND EACH IS LOAD-BEARING.

    1. ASHLAR needs raw, unstitched tiles with stage positions; mirage's registration inputs
       are already-stitched WSIs. ASHLAR_RETILE synthesizes the tile grid. Positions are never
       written to disk: ashlar COMPUTES them as [row, col] * tile_size * (1 - overlap), and
       bin/ashlar_retile.py lays its grid on that same arithmetic, so they agree by
       construction. Every slide is retiled, the reference included, because EdgeAligner must
       stitch the reference grid before LayerAligner can align anything onto it.

    2. ASHLAR's answer is a per-tile PLACEMENT, not a transform object. A per-tile displacement
       on a rectangular grid is a control-grid field, so ASHLAR_SOLVE rewrites it as the M0 +
       mesh manifest TILED_SOLVE already emits (bin/ashlar_solve.py has the derivation).

    3. Because the manifest is byte-compatible, the warp is TILED_STITCH under an alias.
       Nothing about warping an M0 + mesh through bin/tiled_stitch.py is STARE-specific, and
       reusing it is what keeps tiled_stage_warp's promise — "the QC measures exactly the
       transform that shipped" — true for this backend too. Aliasing a process for a second
       caller is the pattern SEG_QC_SEGMENT already uses for SEGMENT.

    WHAT THIS BACKEND CANNOT DO. ASHLAR only ever produces a piecewise TRANSLATION per tile;
    it attempts no non-rigid warp at all. Read its arm against VALIS's *rigid* stage for the
    like-for-like number and against *micro* to quantify what non-rigid buys — reporting only
    the second overstates VALIS's advantage. Its grid granularity (params.reg_ashlar_tile) is
    the direct analogue of STARE's reg_tiled_tile and is a FAIRNESS knob, not just a cost one:
    a finer grid buys ASHLAR more local freedom.

    THE ADAPTER CONTRACT (identical in adapters/valis_adapter.nf and adapters/tiled_adapter.nf,
    and binding here). Takes [patient_id, reference_item, all_items] and emits EXACTLY:

        registered          [meta, file]                registered slides (+ passthroughs)
        transform           [patient_id, transform]     ONE transform object per patient
        transform_by_slide  [meta, transform]           one transform per MOVING slide
        stage_checkpoint    [patient_id, dir]           intermediate-stage fields
        intrinsic_tre       file                        the method's OWN error estimate
        size_logs / versions

    A method that produces no artifact for one of these emits Channel.empty() for it — a NULL
    OBJECT, never a missing emit. ASHLAR composes nothing destructively, so like the tiled
    backend it needs no pre-micro stage checkpoint.
========================================================================================
*/

include { ASHLAR_RETILE } from '../../../modules/local/ashlar_retile'
include { ASHLAR_SOLVE  } from '../../../modules/local/ashlar_solve'
// The warp is STARE's, unchanged — see point 3 in the header.
include { TILED_STITCH as ASHLAR_STITCH } from '../../../modules/local/tiled_stitch'

// The stable per-moving-slide join key, as in tiled_adapter.nf: patient plus the slide's
// channel signature. meta itself gains keys between steps, so it cannot be the key.
def slideKey(meta) {
    return "${meta.patient_id}#${meta.channels.toSorted().join('_')}"
}

// The name a slide carries INSIDE the manifest. Must match TILED_SOLVE's --moving-name
// convention, because bin/warp_seg_qc.py's tiled path reads both slide names out of the
// manifest rather than off its own flags.
def slideName(meta) {
    return meta.channels.join('_')
}


workflow ASHLAR_ADAPTER {
    take:
    ch_grouped_meta   // [patient_id, reference_item, all_items]

    main:
    // The reference is the frame — it passes through unregistered. is_passthrough tells
    // registered_checkpoint.nf that an unwarped reference is published by whoever produced
    // it, not into <pid>/registered/ where no ashlar process writes it. Map addition, never
    // meta.clone() — see the aliasing note at valis_adapter.nf:82-88.
    ch_reference = ch_grouped_meta.map { _pid, ref_item, _items ->
        [ref_item[0] + [is_passthrough: true], ref_item[1]]
    }  // [meta, file]

    // EVERY slide, reference included — see point 1 in the header.
    ch_all_slides = ch_grouped_meta.flatMap { _pid, _ref_item, items ->
        items.collect { item -> tuple(item[0], item[1]) }
    }

    ASHLAR_RETILE(ch_all_slides)

    ch_tiles = ASHLAR_RETILE.out.tiles.branch { meta, _tiles ->
        reference: meta.is_reference
        moving   : true
    }

    // One reference tile-grid per patient, carrying the name the manifest will record.
    ch_ref_tiles = ch_tiles.reference.map { meta, tiles ->
        tuple(meta.patient_id, tiles, slideName(meta))
    }

    // combine(by: 0) rather than join(by: 0): a patient has ONE reference and possibly many
    // moving slides, and combine fans the reference out across all of them. join would pair
    // it with exactly one and silently drop the rest.
    ch_solve_in = ch_tiles.moving
        .map { meta, tiles -> tuple(meta.patient_id, meta, tiles) }
        .combine(ch_ref_tiles, by: 0)
        .map { _pid, meta, mov_tiles, ref_tiles, ref_name ->
            tuple(meta, ref_tiles, mov_tiles, ref_name)
        }

    ASHLAR_SOLVE(ch_solve_in)

    // The warp reads the ORIGINAL stitched moving slide, not the tiles: the tile grid exists
    // only so ashlar could measure the transform. Warping the retiled copy would resample
    // twice and bake the padding into the output.
    ch_moving_files = ch_grouped_meta.flatMap { _pid, _ref_item, items ->
        items.findAll { item -> !item[0].is_reference }
             .collect { mov -> tuple(slideKey(mov[0]), mov[1]) }
    }

    ch_stitch_in = ASHLAR_SOLVE.out.manifest
        .map { meta, m -> tuple(slideKey(meta), meta, m) }
        .join(ch_moving_files, by: 0)
        .map { _k, meta, m, mov -> tuple(meta, m, mov) }

    ASHLAR_STITCH(ch_stitch_in)

    ch_manifest_by_meta = ASHLAR_SOLVE.out.manifest
    ch_versions         = ASHLAR_RETILE.out.versions.first()
        .mix(ASHLAR_SOLVE.out.versions.first())
        .mix(ASHLAR_STITCH.out.versions.first())

    ch_registered = ASHLAR_STITCH.out.registered.mix(ch_reference)

    emit:
    registered       = ch_registered
    // The manifest keyed by patient — the same slot VALIS fills with its registrar pickle.
    transform        = ch_manifest_by_meta.map { meta, m -> tuple(meta.patient_id, m) }
    // The same manifests keyed by meta. Like STARE and unlike VALIS, ashlar has one transform
    // per moving slide, and the reg_qc=2 seg-QC joins them per slide.
    transform_by_slide = ch_manifest_by_meta
    // Nothing is composed destructively, so no pre-micro checkpoint exists: the null object
    // the contract requires.
    stage_checkpoint = Channel.empty()
    // ASHLAR_RETILE is the step that reads the full-resolution slide, so it carries the size
    // log; ASHLAR_STITCH's is dropped to avoid double-counting the same slide.
    size_logs        = ASHLAR_RETILE.out.size_log
    versions         = ch_versions
    // ashlar's own diagnostics: discarded-tile fraction, per-tile alignment error, and the
    // spread of its field about the rigid anchor. A THIRD format again — the channel
    // vocabulary is unified, the file formats are not.
    intrinsic_tre    = ASHLAR_SOLVE.out.tre.map { _meta, f -> f }
}
