/*
========================================================================================
    AdapterContract — the registration-adapter seam, as data
========================================================================================
    THE CONTRACT. Every registration adapter (subworkflows/local/adapters/*_adapter.nf)
    takes `[patient_id, reference_item, all_items]` and emits EXACTLY these names:

        registered          [meta, file]                registered slides (+ passthroughs)
        transform           [patient_id, transform]     the warp artifact, patient-keyed
        transform_by_slide  [meta, transform]           the warp artifact, slide-keyed
        stage_checkpoint    [patient_id, dir]           intermediate-stage fields
        intrinsic_tre       file                        the method's OWN target-registration
                                                        -error estimate, whatever its format
        size_logs           file                        input-size rows for the trace report
        versions            file                        versions.yml, one per process

    A method that produces no artifact for one of these emits `Channel.empty()` — a NULL
    OBJECT, never a missing emit and never an error. That is what lets REGISTER_PATIENT
    wire both backends with one short branch, and it is why nothing downstream of that
    branch has to know which method ran. A future adapter inherits the rule: declare every
    name; empty the ones your method cannot produce.

    `intrinsic_tre` is deliberately NOT named after any one method. Both shipped backends
    estimate a TRE from their own registration -- VALIS a feature-distance CSV, STARE a
    *_tre.json -- and the seam used to call the slot `summary` and then re-emit it as
    `valis_summary`, which pinned one method's name into the artifact vocabulary all the
    way out to the QC report. Formats are NOT normalised here; that is the reader's job.

    WHY THIS IS A CLASS AND NOT A COMMENT. The table above used to be a ~23-line header
    COPIED into both adapter files, identical but for the line naming the other file.
    Nextflow offers no way to declare a subworkflow's emit shape once, so the comment WAS
    the contract, and nothing checked it. Two copies of a table drift, and the copy nobody
    edits is the one the next reader believes. tests/test_adapter_contract.py now checks
    both adapters against THIS file.

    WHY CARDINALITY IS PART OF IT — the defect that motivated the class. Names were never
    the part that varied. `transform` is emitted by both shipped adapters under the same
    name and the same declared `[patient_id, transform]` shape, with OPPOSITE multiplicity:

      * VALIS  `REGISTER.out.registrar` — ONE row per PATIENT. A joint alignment graph
               optimised over the whole group; it does not decompose per slide, which is
               also why VALIS's `transform_by_slide` is the null object.
      * TILED  `TILED_SOLVE.out.manifest` re-keyed to `patient_id` — one row per MOVING
               SLIDE, because TILED_SOLVE runs per slide.

    seg_qc.nf must join those two differently, and it used to choose by string-comparing an
    out-of-band `method` argument against 'tiled'. A third backend that filled `transform`
    per slide, and did not also edit that `if`, would have been combined by patient_id
    alone — and `combine(by:)` is a CROSS JOIN, so N moving slides against N transforms
    yields N**2 pairs, most of them a slide scored against another slide's transform.
    Silently wrong numbers in the QC report, not a crash.

    So consumers ask this table what shape the data has, instead of asking which product
    produced it. Adding a backend means adding a row here; `of()` refuses an unknown one,
    which is the difference between a third adapter that cannot be wired and a third
    adapter that is wired wrongly.

    A CARDINALITY IS AN UPPER BOUND: "AT MOST one row per <unit>". Two of the emits are
    legitimately optional (VALIS's stage_checkpoint exists only at reg_qc >= 2, its
    intrinsic_tre only when REGISTER wrote a summary), and "at most one per patient" is
    exactly the property that makes a by-patient combine safe. Under-production costs an
    artifact; over-production is the N**2 above.

    Everything here is static and nothing reads `params` — `method` arrives as an argument,
    which is what keeps params.registration_method read exactly once on the linear path
    (in subworkflows/local/registration.nf). Same rule as WarpBackends, whose backend list
    tests/test_adapter_contract.py checks against this one and against
    nextflow_schema.json's `registration_method` enum.
========================================================================================
*/

class AdapterContract {

    /* ------------------------------------------------------------------ *
     * The cardinality vocabulary
     * ------------------------------------------------------------------ */

    /** No rows at all: the null object, wired as `Channel.empty()`. */
    static final String NONE = 'none'

    /** At most one row per PATIENT GROUP the adapter was given. */
    static final String PER_PATIENT = 'per_patient'

    /** At most one row per SLIDE in the group, reference included. */
    static final String PER_SLIDE = 'per_slide'

    /** At most one row per NON-REFERENCE slide — the slides that were actually warped. */
    static final String PER_MOVING_SLIDE = 'per_moving_slide'

    /**
     * One row per PROCESS the adapter runs — a constant of the adapter's shape, not a
     * function of how many patients or slides it was handed. `versions` only.
     */
    static final String PER_PROCESS = 'per_process'

    static final List<String> CARDINALITIES = [
        NONE, PER_PATIENT, PER_SLIDE, PER_MOVING_SLIDE, PER_PROCESS,
    ].asImmutable()

    /* ------------------------------------------------------------------ *
     * The emit vocabulary — name to declared tuple shape
     * ------------------------------------------------------------------ */

    static final Map<String, String> EMITS = [
        registered        : '[meta, file]',
        transform         : '[patient_id, transform]',
        transform_by_slide: '[meta, transform]',
        stage_checkpoint  : '[patient_id, dir]',
        intrinsic_tre     : 'file',
        size_logs         : 'file',
        versions          : 'file',
    ].asImmutable()

    /* ------------------------------------------------------------------ *
     * Per-backend declarations
     * ------------------------------------------------------------------ */

    /**
     * What each shipped adapter puts on each channel. Keyed by the method name the
     * pipeline knows the backend as (`--registration_method`), which is also the
     * `<method>_adapter.nf` filename stem — tests/test_adapter_contract.py checks that
     * correspondence in both directions, so an adapter file with no row here, or a row
     * here with no adapter file, is a failure rather than a surprise at runtime.
     */
    private static final Map<String, Map<String, String>> BACKENDS = [
        valis: [
            // REGISTER hands back every slide of the group, reference included: VALIS
            // re-writes the reference into the registered frame like any other slide.
            registered        : PER_SLIDE,
            // The registrar pickle: ONE joint graph for the patient. This is the
            // cardinality seg_qc.nf's combine-by-patient depends on.
            transform         : PER_PATIENT,
            // Not decomposable per slide — the null object, not an omission.
            transform_by_slide: NONE,
            // reg_stage_checkpoint/, written only at reg_qc >= 2. Optional, hence "at most".
            stage_checkpoint  : PER_PATIENT,
            // preprocessed/data/*_summary.csv — REGISTER's own feature distances. Also
            // optional (`optional: true` on the process output).
            intrinsic_tre     : PER_PATIENT,
            // REGISTER is one task per patient, so its size row is too.
            size_logs         : PER_PATIENT,
            versions          : PER_PROCESS,
        ],
        tiled: [
            // TILED_STITCH's warped moving slides, mixed with the reference the method
            // passes through unwarped — the group's every slide, as under VALIS.
            registered        : PER_SLIDE,
            // THE CRUX: the same emit name as VALIS's per-patient registrar, but one row
            // per moving slide, merely re-keyed to patient_id. A consumer that joins this
            // by patient_id alone cross-joins.
            transform         : PER_MOVING_SLIDE,
            // The same manifests keyed by meta; the reg_qc=2 seg-QC joins them per slide.
            transform_by_slide: PER_MOVING_SLIDE,
            // The tiled method composes no stages destructively, so it needs no pre-micro
            // checkpoint at all.
            stage_checkpoint  : NONE,
            // TILED_SOLVE's *_tre.json — a DIFFERENT format from VALIS's CSV, on purpose.
            intrinsic_tre     : PER_MOVING_SLIDE,
            // TILED_STITCH runs per moving slide.
            size_logs         : PER_MOVING_SLIDE,
            versions          : PER_PROCESS,
        ],
    ].asImmutable()

    /* ------------------------------------------------------------------ *
     * Lookups
     * ------------------------------------------------------------------ */

    /** Every backend name this seam knows. */
    static List<String> methods() {
        return BACKENDS.keySet() as List
    }

    /**
     * The declaration for one backend: `[method: ..., emits: [emit: cardinality, ...]]`.
     *
     * THIS IS THE REFUSAL POINT for a third backend that was added to the schema enum and
     * an adapter file but never described here — it fails at wiring time with a message
     * naming what is missing, rather than being joined on whichever shape the consumer
     * happened to assume.
     */
    static Map of(String method) {
        def emits = BACKENDS[method]
        if (!emits)
            throw new IllegalArgumentException(
                "No adapter contract declared for registration method '${method}'. " +
                "Declared: ${methods()}. Add its emit cardinalities to " +
                "lib/AdapterContract.groovy before wiring the adapter.")
        return [method: method, emits: emits].asImmutable()
    }

    /** The declared cardinality of one emit, from a contract returned by {@link #of}. */
    static String cardinalityOf(Map contract, String emit) {
        def cardinality = contract?.emits?.get(emit)
        if (!cardinality)
            throw new IllegalArgumentException(
                "Adapter contract for '${contract?.method}' declares nothing for emit " +
                "'${emit}'. Declared emits: ${EMITS.keySet() as List}.")
        return cardinality
    }

    /** Convenience overload for callers that hold the method name rather than the contract. */
    static String cardinalityOf(String method, String emit) {
        return cardinalityOf(of(method), emit)
    }

    /**
     * True when `emit` carries one row per slide rather than one per patient.
     *
     * THE ONE QUESTION CONSUMERS ASK. seg_qc.nf branches on this for `transform`: a
     * per-slide transform must be joined on a compound (patient, slide) key, a per-patient
     * one on patient_id alone. Asking here rather than comparing the backend's name is
     * what makes a third backend's join shape follow from its declaration.
     */
    static boolean isPerSlide(Map contract, String emit) {
        def cardinality = cardinalityOf(contract, emit)
        return cardinality == PER_SLIDE || cardinality == PER_MOVING_SLIDE
    }

    /**
     * How many rows `emit` may carry for a group of the given shape, or -1 when the
     * declaration is not a function of that shape (PER_PROCESS).
     *
     * `shape` is `[patients: n, slides: n, moving_slides: n]`. Test-facing: it is what
     * tests/subworkflows/local/adapters/adapter_cardinality_probe.nf compares an adapter's
     * observed row counts against. Nothing in the pipeline calls it — counting a stream
     * would mean buffering it, which is exactly the streaming property the sized
     * groupTuples elsewhere exist to preserve.
     */
    static int expectedCount(String method, String emit, Map shape) {
        switch (cardinalityOf(method, emit)) {
            case NONE:             return 0
            case PER_PATIENT:      return shape.patients as int
            case PER_SLIDE:        return shape.slides as int
            case PER_MOVING_SLIDE: return shape.moving_slides as int
            default:               return -1
        }
    }
}
