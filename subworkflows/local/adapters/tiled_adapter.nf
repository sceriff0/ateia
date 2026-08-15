/*
========================================================================================
    TILED (STARE) REGISTRATION ADAPTER
========================================================================================
    Converts the patient-grouped structure into the tiled method's per-moving-slide star:
    every moving slide registers directly to the fixed reference (which defines the frame),
    independently and in parallel. Unlike the VALIS adapter there is no batch graph build and
    no per-slide OME-channel re-matching — each task already carries its slide's meta.

    ONE execution shape, same channel contract as VALIS_ADAPTER: a per-TILE Nextflow fan-out —
    TILED_COARSE -> TILED_REG_TILE (one task per tile) -> TILED_SOLVE -> TILED_STITCH. It
    maximises parallelism, and every process' peak memory is a function of a parameter
    (reg_tiled_coarse_max_dim, tile+halo, out_tile) rather than of the slide's dimensions.

    A second, single-task shape used to sit behind a boolean flag. Both the flag and that process
    were removed, because it could not hold that bound — it kept both whole slides, an
    all-channel float32 copy and the full warped output live at once, so its memory scaled with
    the input and had to be budgeted from file size. Keeping an unbounded alternative behind a
    flag was shipping a footgun. See CHANGELOG (Unreleased -> Removed).

    Input:  ch_grouped_meta - Channel of [patient_id, reference_item, all_items]

    THE ADAPTER CONTRACT lives in lib/AdapterContract.groovy, not here. It declares the
    emit names every adapter must fill, the tuple shape of each, and -- the part a comment
    kept getting wrong -- the CARDINALITY each one carries under this backend. It used to
    be a ~23-line table copied verbatim into both adapter files, so it was two tables, and
    it declared only names -- while the emit named `transform` carries ONE ROW PER MOVING
    SLIDE here and one row per PATIENT under the VALIS adapter, which is the fact consumers
    actually branch on.
    tests/test_adapter_contract.py checks this file against that declaration, and
    tests/subworkflows/local/adapters/adapter_cardinality.nf.test counts what it really
    emits. Adding an emit, or a third backend, starts there.
========================================================================================
*/

include { TILED_COARSE   } from '../../../modules/local/tiled_coarse'
include { TILED_REG_TILE } from '../../../modules/local/tiled_reg_tile'
include { TILED_SOLVE    } from '../../../modules/local/tiled_solve'
include { TILED_STITCH   } from '../../../modules/local/tiled_stitch'

// Stable per-moving-slide join key (patient + its channel set).
def slideKey(meta) { "${meta.patient_id}#${meta.channels.toSorted().join('_')}" }

// Counts a tile-plan CSV's data rows: total non-blank lines minus the header. Extracted (not
// left inline in the ch_tile_counts closure below) so the counting logic itself is unit-testable
// in isolation -- this is the number that ends up sizing the groupKey at the gather, so an
// off-by-one here either hangs the pipeline (count too high, the group never closes) or
// truncates the control points TILED_SOLVE fits its mesh from (count too low, the group closes
// early on a silently mis-registered slide). Neither failure mode looks like a crash, which is
// exactly why the count itself -- not just PatientGroup's mandatory-size check at the gather --
// needs direct coverage.
def countTileRows(csv) {
    csv.readLines().findAll { it.trim() }.size() - 1
}

// Fails loudly (naming the slide) on an empty tile plan, mirroring PatientGroup's
// never-silently-degrade contract for the other half of the count derivation: the gather
// refuses a MISSING tiles_count, this refuses a tile plan that would derive one of zero.
def requirePositiveTileCount(n, meta) {
    if (n < 1) error "TILED_COARSE emitted no tiles for ${slideKey(meta)} (n=${n})"
    n
}

workflow TILED_ADAPTER {
    take:
    ch_grouped_meta   // [patient_id, reference_item, all_items]

    main:
    // The reference is the frame — it passes through unregistered. is_passthrough says
    // so to registration.nf's checkpoint writer: an unwarped reference is published by
    // whoever produced it, not into <pid>/registered/ where no tiled process writes it.
    // Map addition, never meta.clone() — see the aliasing note at valis_adapter.nf:82-88.
    ch_reference = ch_grouped_meta.map { _pid, ref_item, _items ->
        [ref_item[0] + [is_passthrough: true], ref_item[1]]
    }  // [meta, file]

    // One stream item per moving (non-reference) slide: [meta, reference_file, moving_file].
    ch_moving = ch_grouped_meta.flatMap { _pid, ref_item, items ->
        def ref_file = ref_item[1]
        items.findAll { item -> !item[0].is_reference }
             .collect { mov -> tuple(mov[0], ref_file, mov[1]) }
    }

    TILED_COARSE(ch_moving)

    // Tile count per slide, derived from the tile-plan CSV file itself -- NOT from the
    // splitCsv rows below: splitCsv streams one item per row, so the closure that sees a
    // row cannot know how many rows the file has in total. The CSV is tiny (one row per
    // tile, plus a header), so counting lines is cheap.
    ch_tile_counts = TILED_COARSE.out.tiles
        .map { meta, csv -> tuple(slideKey(meta), requirePositiveTileCount(countTileRows(csv), meta)) }

    // Context per slide: meta (+tiles_count) + reference + moving + M0, keyed for the
    // per-tile join. Map addition (never meta.clone()) -- see the aliasing note at
    // valis_adapter.nf:82-88.
    ch_ctx = ch_moving
        .map { meta, ref, mov -> tuple(slideKey(meta), meta, ref, mov) }
        .join(TILED_COARSE.out.m0.map { meta, m0 -> tuple(slideKey(meta), m0) }, by: 0)
        .join(ch_tile_counts, by: 0)
        .map { k, meta, ref, mov, m0, n -> tuple(k, meta + [tiles_count: n], ref, mov, m0) }

    // Fan out: one item per tile (splitCsv), attach the slide context.
    ch_tile_items = TILED_COARSE.out.tiles
        .splitCsv(header: true, elem: 1)
        .map { meta, row -> tuple(slideKey(meta), row) }
        .combine(ch_ctx, by: 0)
        .map { _k, row, meta, ref, mov, m0 -> tuple(meta, m0, ref, mov, row) }

    TILED_REG_TILE(ch_tile_items)

    // Gather every tile's control point back per slide. The size is the slide's own
    // tile count, so its group closes -- and TILED_SOLVE starts -- as soon as THAT
    // slide's tiles have arrived, instead of waiting for every tile of every slide of
    // every patient. This is the one gather in the pipeline whose unit is a slide
    // rather than a patient, hence byKey rather than byPatient. The mandatory-size
    // rule that used to live here as `requireTilesCount` is lib/PatientGroup.groovy's
    // now, and applies to every grouping instead of only this one.
    ch_controls = PatientGroup.byKey(
            TILED_REG_TILE.out.control,
            name  : 'TILED_ADAPTER: the per-slide control-point gather feeding TILED_SOLVE',
            size  : 'tiles_count',
            key   : { meta -> slideKey(meta) },
            sortBy: { _meta, control -> control.name },
        )
        .map { _k, pairs -> tuple(pairs[0][0], pairs.collect { pair -> pair[1] }) }

    ch_solve_in = ch_controls
        .map { meta, controls -> tuple(slideKey(meta), meta, controls) }
        .join(TILED_COARSE.out.m0.map { meta, m0 -> tuple(slideKey(meta), m0) }, by: 0)
        .map { _k, meta, controls, m0 -> tuple(meta, m0, controls) }

    TILED_SOLVE(ch_solve_in)

    ch_stitch_in = TILED_SOLVE.out.manifest
        .map { meta, m -> tuple(slideKey(meta), meta, m) }
        .join(ch_moving.map { meta, _ref, mov -> tuple(slideKey(meta), mov) }, by: 0)
        .map { _k, meta, m, mov -> tuple(meta, m, mov) }

    TILED_STITCH(ch_stitch_in)

    ch_registered_moving = TILED_STITCH.out.registered
    ch_manifest_by_meta  = TILED_SOLVE.out.manifest
    ch_size_logs         = TILED_STITCH.out.size_log
    ch_versions          = TILED_COARSE.out.versions.first()
        .mix(TILED_REG_TILE.out.versions.first())
        .mix(TILED_SOLVE.out.versions.first())
        .mix(TILED_STITCH.out.versions.first())
    ch_intrinsic_tre     = TILED_SOLVE.out.tre.map { _meta, f -> f }

    ch_registered = ch_registered_moving.mix(ch_reference)

    emit:
    registered       = ch_registered
    // The STARE transform manifest keyed by patient — the same slot VALIS fills with its
    // registrar pickle. A patient with several moving slides contributes several items here.
    transform        = ch_manifest_by_meta.map { meta, m -> tuple(meta.patient_id, m) }
    // The same manifests keyed by meta. Unlike VALIS, the tiled method DOES have one transform
    // per moving slide, and the reg_qc=2 seg-QC joins them per slide.
    transform_by_slide = ch_manifest_by_meta
    // The tiled method composes no stages destructively, so it needs no pre-micro checkpoint:
    // the null object the contract above requires.
    stage_checkpoint = Channel.empty()
    size_logs        = ch_size_logs
    versions         = ch_versions
    // STARE's intrinsic TRE: *_tre.json from bin/utils/tre_report.py. A DIFFERENT format
    // from VALIS's CSV, on purpose — the channel vocabulary is unified, the file formats
    // are not, and teaching the report reader both shapes is a separate change.
    intrinsic_tre    = ch_intrinsic_tre
}
