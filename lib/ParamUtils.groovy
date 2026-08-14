/*
 * ParamUtils - validation helpers for the pipeline's step-routing parameters.
 *
 * Scope note: single-parameter checks (is this a valid step name? a valid
 * segmentation backend? a reg_qc level in range?) are NOT here. They are stated
 * once in nextflow_schema.json and enforced by nf-schema's validateParameters()
 * at the top of workflows/mirage.nf. What remains below is what no JSON Schema
 * can express: cross-parameter rules (--stop must not precede --start,
 * --quantify_statistics with Mean+Sum implies --quantify_compartments), mode-conditional
 * rules (add_cycle), filesystem prerequisites, and plain control-flow helpers.
 */
class ParamUtils {

    /*
     * The single table answering "what is a step?". Everything else that used
     * to restate the step vocabulary — STEP_ORDER, requiredColumnsForStep,
     * mirage.nf's entry_column map, final_qc.nf's KNOWN_ARTIFACT_KINDS, and
     * nextflow_schema.json's start/stop enums — is either derived from this or
     * checked against it (the schema can't be generated at build time, so
     * tests/test_step_vocabulary_consistency.py asserts it agrees instead).
     *
     *   name            - the step's canonical identifier; also the value
     *                      --start/--stop accept.
     *   requiredColumns - samplesheet columns CsvUtils must find when this step
     *                      is the run's entry point (its own input, not a
     *                      downstream checkpoint column).
     *   entryColumn     - the checkpoint-CSV column INPUT_CHECK reads when this
     *                      step is the run's entry point (mirage.nf reads the
     *                      sheet exactly once, at --start).
     *   qcKinds         - the artifact-stream tags this step alone contributes
     *                      to FINAL_QC (see final_qc.nf). 'versions' and
     *                      'size_log' are cross-cutting — every step emits them
     *                      — so they are not listed per-step; they are added
     *                      once in UNIVERSAL_QC_KINDS below.
     */
    static final List STEPS = [
        [
            name           : 'preprocessing',
            requiredColumns: ['patient_id', 'path_to_file', 'is_reference', 'channels'],
            entryColumn    : 'path_to_file',
            qcKinds        : ['preprocess_qc'],
        ],
        [
            name           : 'registration',
            requiredColumns: ['patient_id', 'preprocessed_image', 'is_reference', 'channels'],
            entryColumn    : 'preprocessed_image',
            qcKinds        : ['registration_qc', 'registration_tre', 'seg_qc', 'seg_residuals'],
        ],
        [
            name           : 'segmentation',
            requiredColumns: ['patient_id', 'registered_image', 'is_reference', 'channels'],
            entryColumn    : 'registered_image',
            // Empty by design, not an oversight: SEGMENT and the two property
            // extractors emit only 'versions' and 'size_log', which are
            // UNIVERSAL_QC_KINDS below, so they contribute nothing here. No QC
            // image belongs to segmentation alone — GENERATE_POSTPROCESSING_QC
            // consumes the mask AND the merged quantification table, so it stays
            // postprocessing's.
            qcKinds        : [],
        ],
        [
            name           : 'postprocessing',
            // 'cell_mask' and 'nuclei_mask' (beyond the registered-image base four) make
            // a plain csv/registered.csv -- still what every doc's `--start postprocessing`
            // example names, pre-dating the segmentation step -- fail HERE with a
            // clear "Missing required column" error. Without them, CsvUtils.validateInputCSV
            // and validateInputSemantics both pass (registered.csv satisfies the base
            // four), and the run dies much later inside segmentation.nf's
            // READ_SEGMENTED_CHECKPOINT -- which dereferences BOTH file(row.cell_mask) and
            // file(row.nuclei_mask) unconditionally -- with "Argument of 'file' function cannot be
            // null" -- a two-layer validation contract that silently skipped its job.
            requiredColumns: ['patient_id', 'registered_image', 'is_reference', 'channels', 'cell_mask', 'nuclei_mask'],
            entryColumn    : 'registered_image',
            qcKinds        : ['postprocess_qc'],
        ],
    ]

    // Artifact-stream tags every step contributes, so they belong to no single
    // step's qcKinds. See final_qc.nf's KNOWN_ARTIFACT_KINDS derivation.
    static final List UNIVERSAL_QC_KINDS = ['versions', 'size_log']

    static final List STEP_ORDER = STEPS.collect { it.name }

    static void validateOutdir(String outdir) {
        if (!outdir?.trim()) {
            throw new IllegalArgumentException(
                "Please provide --outdir (the pipeline's output directory). " +
                "Without it every published file lands under a literal 'null/' path.")
        }
    }

    /**
     * Ordering-only check: --stop must not name an earlier step than --start.
     * That both are valid step names is asserted by the schema before this runs.
     */
    static void validateStop(String stop, String start) {
        if (STEP_ORDER.indexOf(stop) < STEP_ORDER.indexOf(start)) {
            throw new IllegalArgumentException("--stop '${stop}' cannot come before --start '${start}'. Pipeline order: ${STEP_ORDER.join(' → ')}")
        }
    }

    static void validateAddCycle(String outdir, String priorOutdir) {
        if (!priorOutdir?.trim()) {
            throw new IllegalArgumentException(
                "mode='add_cycle' requires --prior_outdir pointing at the previous run's --outdir")
        }
        // add_cycle writes its registered-checkpoint CSV via collectFile(storeDir:),
        // which OVERWRITES. If --outdir resolved to the same directory as
        // --prior_outdir, that write would clobber the prior run's complete
        // manifest with add_cycle's partial one, while the postprocessed-checkpoint
        // CSV (untouched by add_cycle) survives — leaving --prior_outdir internally
        // inconsistent and unrecoverable. Compare canonical paths, not raw strings:
        // a trailing slash, a '.', or a symlink must not defeat this check.
        if (outdir?.trim()) {
            def outdirCanonical = new File(outdir).canonicalPath
            def priorOutdirCanonical = new File(priorOutdir).canonicalPath
            if (outdirCanonical == priorOutdirCanonical) {
                def registeredRel = Layout.checkpointCsvRelative(Layout.REGISTERED)
                def postprocessedRel = Layout.checkpointCsvRelative(Layout.POSTPROCESSED)
                throw new IllegalArgumentException(
                    "mode='add_cycle': --outdir must not be the same directory as --prior_outdir " +
                    "('${priorOutdir}'). add_cycle's '${registeredRel}' checkpoint write overwrites " +
                    "in place, which would clobber the prior run's manifest while '${postprocessedRel}' " +
                    "survives untouched, leaving --prior_outdir internally inconsistent. Use a FRESH " +
                    "--outdir for the incremental run, as docs/add_cycle.md describes.")
            }
        }
        // Which checkpoints a prior run must have left behind, and where they live,
        // is Layout's to state — add_cycle.nf reads the very same two files.
        Layout.ADD_CYCLE_CHECKPOINTS.each { step ->
            def rel = Layout.checkpointCsvRelative(step)
            def f = new File(Layout.checkpointCsv(priorOutdir, step))
            if (!f.exists()) {
                throw new FileNotFoundException(
                    "mode='add_cycle': required checkpoint '${rel}' not found under --prior_outdir '${priorOutdir}'. " +
                    "Was the prior run completed through postprocessing?")
            }
        }
    }

    /**
     * Check whether a given pipeline step should run, based on --start and --stop.
     */
    static boolean shouldRun(String targetStep, String start, String stop) {
        def idx = STEP_ORDER.indexOf(targetStep)
        if (idx == -1) throw new IllegalArgumentException("Unknown step: '${targetStep}'. Valid: ${STEP_ORDER}")
        return idx >= STEP_ORDER.indexOf(start) && idx <= STEP_ORDER.indexOf(stop)
    }

    /**
     * Effective registration-QC depth: 0 = none, 1 = DAPI overlay, 2 = + segmentation overlap.
     * Legacy skip_registration_qc=true forces 0. Defined once and shared by
     * registration.nf / add_cycle.nf so the QC gate has a single source of truth.
     */
    /*
     * WHY regQcLevelOf / microRegLevelOf ARE SCALAR
     *
     * Nextflow hashes the FREE VARIABLES a process's `script:` block references. A block
     * that says `ParamUtils.regQcLevel(params)` therefore hashes `params` -- the WHOLE
     * map, as one opaque entry -- and re-runs whenever ANY parameter anywhere changes.
     * Measured: changing only `--pyramid_resolutions` (postprocessing) re-ran REGISTER and
     * cascaded 20 of 28 tasks. `-dump-hashes` showed every individually-named entry
     * unchanged and only `params=ScriptBinding$ParamsMap@...` differing.
     *
     * A `params.reg_qc` / `params.reg_micro_reg` access is recorded as its OWN hash entry,
     * so passing the scalar keeps the process bound to just the parameters it actually reads.
     *
     * regQcLevel(Map) stays: workflow/subworkflow code (registration.nf, add_cycle.nf) is
     * not hashed this way, reads better with `params`, and there is exactly one
     * implementation behind it -- the scalar form is where the logic lives and the Map
     * form delegates to it, so the two cannot drift. `microRegLevelOf` has no Map
     * counterpart -- every call site is inside a `script:` block (register.nf,
     * warp_seg_qc.nf), so there is no workflow-level caller to justify one. CALL THE
     * `...Of` FORM FROM A `script:` BLOCK; either form elsewhere.
     *
     * `regQcLevelOf` is named rather than being an overload of `regQcLevel(Map)` because
     * `regQcLevel(Map)` and a same-arity `regQcLevel(def)` are ambiguous for a NULL
     * argument -- and null is the expected input here, it is what selects the default.
     * Groovy would pick the more specific Map candidate and NPE on `params.reg_qc`. A
     * distinct name removes the dispatch question entirely.
     */
    static int regQcLevelOf(def skipRegistrationQc, def regQc) {
        return skipRegistrationQc ? 0 : (regQc == null ? 2 : (regQc as int))
    }

    /** Map form -- see the note above. Delegates; holds no logic of its own. */
    static int regQcLevel(Map params) {
        return regQcLevelOf(params.skip_registration_qc, params.reg_qc)
    }

    /**
     * Effective micro-registration depth: 0 = none, 1 = micro-rigid only (refines slide.M),
     * 2 = micro-rigid + micro non-rigid (register_micro). VALIS controls the two passes
     * independently — micro_rigid_registrar_cls for the rigid refinement, the register_micro()
     * call for the non-rigid one — and this ordinal nests them (0 ⊂ 1 ⊂ 2), which forbids the
     * odd "micro non-rigid without micro-rigid" combination. Default 2 (max: micro-rigid +
     * micro non-rigid), matching nextflow.config. Single source of truth for register.nf /
     * warp_seg_qc.nf so the QC can honestly say what the 'rigid' stage means for a given run.
     */
    static int microRegLevelOf(def regMicroReg) {
        return regMicroReg == null ? 2 : (regMicroReg as int)
    }

    /**
     * add_cycle does NOT run phenotyping: the incremental subworkflow has no
     * COMPILE_PANEL/PHENOTYPE, and EXPORT_GEOJSON reuses the cell-contours file as a
     * placeholder in the phenotype/model_config slots. If a panel were configured,
     * EXPORT_GEOJSON's arg guard (params.panel_spec || params.panel_model) would activate
     * --phenotypes/--panel_model against that placeholder and mis-classify. Reject the
     * combination at launch rather than silently emit a wrong cells.geojson.
     */
    static void validateAddCyclePhenotyping(Map params) {
        if (params.panel_spec || params.panel_model) {
            throw new IllegalArgumentException(
                "mode='add_cycle' does not support phenotyping. Unset --panel_spec / --panel_model " +
                "for the incremental run — the new cycle inherits the base run's classification.")
        }
    }

    /**
     * add_cycle runs a FIXED path — new-cycle samplesheet -> preprocess -> register against
     * the frozen prior reference -> quantify -> export — there is no --start/--stop choice
     * to make, and 'add_cycle' is not itself a member of STEP_ORDER (it is a mode, not a
     * step; see the header comment on STEPS). Accepting either flag and silently ignoring it
     * used to let a run report whatever --stop the caller typed as though it had been
     * honoured (e.g. --stop registration completing the FULL path through export while
     * run_summary.json claimed the run stopped after registration) — accept-and-ignore is
     * the defect, not the label, so this rejects both rather than trying to describe
     * whatever was ignored.
     *
     * --stop defaults to null, so an explicit --stop is exactly `params.stop != null`.
     * --start defaults to 'preprocessing', so an explicit non-default --start is exactly
     * `params.start != 'preprocessing'` — a caller who explicitly passes
     * --start preprocessing is indistinguishable from one who passed neither flag, and
     * that ambiguity is harmless: 'preprocessing' is also the only correct description of
     * where add_cycle's own fixed path begins.
     */
    static void validateAddCycleStepFlags(Map params) {
        if (params.stop != null || params.start != 'preprocessing') {
            throw new IllegalArgumentException(
                "mode='add_cycle' runs a fixed path from the new-cycle samplesheet through " +
                "export (preprocess -> register against the frozen prior reference -> " +
                "quantify -> export) — --start/--stop do not apply in this mode and must be omitted.")
        }
    }

    /**
     * Resolve --quantify_compartments / --quantify_statistics / --embed_masks
     * into ONE immutable snapshot, the same seam --registration_method has
     * (subworkflows/local/registration.nf: read once, threaded down as an
     * argument, never re-read). Call this once per top-level entry point
     * (workflows/mirage.nf) and pass the returned map down to any subworkflow
     * that takes it as a `take:` argument or used to re-derive these booleans
     * itself (subworkflows/local/segmentation.nf's two workflows — SEGMENTATION
     * (`take:`, segmentation.nf:42) and READ_SEGMENTED_CHECKPOINT (`take:`,
     * segmentation.nf:275) — plus postprocess.nf, add_cycle.nf,
     * assemble_export.nf). Config
     * (`conf/modules.config`'s `ext.args`) and module `script:`/`stub:` blocks
     * (e.g. modules/local/quantify.nf) are explicitly out of scope: config cannot
     * take arguments or see this class, and a module reading its own param to
     * build its own flags is this repo's established pattern for both.
     * tests/test_compartment_mode_routing.py enforces that nothing else reads
     * the three raw params directly.
     */
    /**
     * The statistic vocabulary, mirroring bin/utils/measurements.py's STATISTICS.
     *
     * COMPOSED from base x normalisation, exactly as bin/utils/measurements.py
     * composes it -- twelve names written out by hand in two languages is how two
     * of them end up spelled differently.
     * tests/test_statistic_vocabulary.py asserts this list, measurements.STATISTICS
     * and nextflow_schema.json's enum are the same set.
     *
     * Canonical ORDER, not just membership: columns are emitted in this order so two
     * runs asking for {Sum, Median} and {Median, Sum} produce identical output.
     * REDSEA is the only WHOLE-CELL-ONLY base -- the compensation is a membrane
     * correction with no nucleus/cytoplasm decomposition. The " Z" / " RobustZ"
     * variants standardise across ONE PATIENT's cells.
     */
    static final List<String> BASE_STATISTICS = ['Median', 'Mean', 'Sum', 'REDSEA'].asImmutable()
    static final List<String> NORMALIZATIONS = ['', ' Z', ' RobustZ'].asImmutable()
    static final List<String> STATISTICS =
        BASE_STATISTICS.collectMany { base -> NORMALIZATIONS.collect { base + it } }.asImmutable()

    /**
     * THE ONLY WAY TO READ params.quantify_statistics.
     *
     * Same three-shape problem MarkerUtils.markerList documents for
     * params.nuclear_markers, and the same two silent failures:
     *
     *   ['Median','REDSEA']  nextflow.config / params file   - the intended shape
     *   'REDSEA'             `--quantify_statistics REDSEA` on the CLI (an array
     *                        cannot be given on a command line)
     *   ['Median,REDSEA']    a params file with the list written as one comma string
     *
     * The bare String iterated as a List yields the CHARACTERS R,E,D,S,E,A. The
     * one-element comma string is a valid Collection so nothing coerces it, and the
     * single entry 'Median,REDSEA' matches no known statistic.
     *
     * An unknown name THROWS rather than being dropped: a typo'd 'Meidan' that
     * silently produced no Median column would surface months later as missing data,
     * not as an error. nextflow_schema.json enums the ARRAY form, but it cannot enum
     * the string form (a `--quantify_statistics Median,REDSEA` command line), so this
     * check is genuinely reachable rather than a duplicate of the schema.
     */
    static List<String> statisticsList(def statistics) {
        def raw
        if (statistics == null)                      raw = []
        else if (statistics instanceof CharSequence) raw = [statistics]
        else if (statistics instanceof Object[])     raw = statistics as List
        else if (statistics instanceof Collection)   raw = statistics as List
        else                                         raw = [statistics]

        def asked = raw
            // COMMA only, never whitespace -- composed names carry a space
            // ("Mean Z", "Median RobustZ"), and a /[,\s]+/ split would shred them
            // into an unknown statistic "Z".
            .collectMany { it == null ? [] : it.toString().split(/,/) as List }
            .collect { it?.trim() }
            .findAll { it }
        if (!asked) {
            throw new IllegalArgumentException(
                "No statistics configured. Pass params.quantify_statistics; its " +
                "default lives in nextflow.config and nowhere else. Valid: " +
                STATISTICS.join(', ')
            )
        }
        def byUpper = STATISTICS.collectEntries { [(it.toUpperCase()): it] }
        def chosen = [] as Set
        asked.each { name ->
            def hit = byUpper[name.toUpperCase()]
            if (!hit) {
                throw new IllegalArgumentException(
                    "Unknown statistic '${name}' in --quantify_statistics. Valid: " +
                    STATISTICS.join(', ')
                )
            }
            chosen << hit
        }
        return STATISTICS.findAll { chosen.contains(it) }
    }

    static Map compartmentMode(Map params) {
        def stats = statisticsList(params.quantify_statistics)
        return [
            compartments: params.quantify_compartments as boolean,
            statistics  : stats.asImmutable(),
            // DERIVED, not a parameter. `expanded` used to be its own boolean
            // meaning "Mean and Sum as well as Median"; it survives only as the
            // exact predicate the embed_masks gate and the run-summary field
            // already keyed on, so neither changes behaviour as a side effect of
            // the parameter becoming a list.
            expanded    : stats.contains('Mean') && stats.contains('Sum'),
            embedMasks  : params.embed_masks as boolean,
        ].asImmutable()
    }

    /**
     * Cross-parameter rules for the compartment-quantification trio, both derived
     * from `mode` (see compartmentMode above) rather than read raw:
     *
     *   expanded ⇒ compartments      (long-standing: per-compartment Mean/Sum
     *                                 needs a per-compartment signal to sum)
     *   embedMasks ⇒ compartments && expanded
     *                                 (assemble_export.nf's embed_masks gate --
     *                                 `params.embed_masks && params.quantify_compartments
     *                                 && Mean+Sum in quantify_statistics` -- decides
     *                                 whether the pyramid gets a mask series (Image:1).
     *                                 --embed_masks true with either sibling off used to
     *                                 exit 0 and silently publish a pyramid with NO mask
     *                                 series; that run only fails months later, when its
     *                                 --outdir is handed to a --prior_outdir add_cycle run
     *                                 and EXTRACT_MASK_SERIES finds no Image:1.)
     */
    static void validateCompartmentQuant(Map mode) {
        if (mode.expanded && !mode.compartments) {
            throw new IllegalArgumentException(
                "--quantify_statistics asks for both Mean and Sum, which requires " +
                "--quantify_compartments to be true."
            )
        }
        if (mode.embedMasks && !(mode.compartments && mode.expanded)) {
            throw new IllegalArgumentException(
                "--embed_masks requires both --quantify_compartments and " +
                "Mean and Sum in --quantify_statistics -- without both, the pyramid's " +
                "mask series (Image:1) is never written, and a run advertising " +
                "--embed_masks that silently omits it is only discovered later, when " +
                "this --outdir is handed to mode='add_cycle' as --prior_outdir and " +
                "EXTRACT_MASK_SERIES finds no Image:1 to reuse."
            )
        }
    }

    /**
     * The REDSEA trio, resolved once — same shape as compartmentMode above.
     *
     * There is deliberately NO `params.redsea` boolean: listing 'REDSEA' in
     * params.quantify_statistics is the switch. Two parameters expressing one fact
     * is the duplication that lets them disagree.
     *
     * REDSEA (Bai et al., Front. Immunol. 2021;12:652631) is read exactly once on
     * the quantification path, in subworkflows/local/quantify_markers.nf, and
     * passed down as data. This method exists so the CROSS-parameter rules below
     * run at validation time, before any process is instantiated, rather than
     * surfacing as an empty column set at the end of a long run.
     */
    static Map redseaMode(Map params) {
        return [
            enabled: statisticsList(params.quantify_statistics).contains('REDSEA'),
            markers: params.redsea_markers,
        ].asImmutable()
    }

    /**
     * The ONE cross-parameter rule REDSEA has: enabled ⇒ a non-empty marker list.
     *
     * REDSEA with no markers runs REDSEA_MATRIX for every patient — the whole mask
     * pass — and then compensates nothing. The run is green, every output is
     * byte-identical to a `--redsea false` run, and the only symptom is a missing
     * set of columns nobody thinks to look for.
     *
     * Nothing else belongs here. `--redsea_element_size` must be a positive integer
     * and `--redsea_element_shape` must be one of three names, but those are
     * SINGLE-parameter range/enum rules, so nextflow_schema.json owns them and
     * `validateParameters()` runs first — a copy here would be a guard that can
     * never fire (verified: `--redsea_element_size 0` is rejected by the schema
     * with "0 is less than 1" before this method is reached).
     */
    static void validateRedsea(Map mode) {
        if (!mode.enabled) return

        def markers = mode.markers
        def flat = (markers instanceof CharSequence) ? [markers]
                 : (markers instanceof Object[])     ? (markers as List)
                 : (markers instanceof Collection)   ? (markers as List)
                 : (markers == null ? [] : [markers])
        def names = flat
            .collectMany { it == null ? [] : it.toString().split(/[,]/) as List }
            .collect { it?.trim() }
            .findAll { it }
        if (!names) {
            throw new IllegalArgumentException(
                "'REDSEA' is in --quantify_statistics but --redsea_markers is empty. REDSEA is opt-in per " +
                "marker because it is only valid for SURFACE/MEMBRANE markers: the " +
                "method assumes the signal sits on the membrane and is brighter in " +
                "the source cell than in the leak. With no markers the run would " +
                "compute the compensation geometry for every patient and then " +
                "compensate nothing, producing output identical to omitting it " +
                "with no error anywhere. Name the surface markers, e.g. " +
                "--redsea_markers CD3,CD8,CD20, or drop REDSEA from " +
                "--quantify_statistics."
            )
        }

    }

    static List requiredColumnsForStep(String step) {
        def entry = STEPS.find { it.name == step }
        if (!entry) {
            throw new IllegalArgumentException("No column requirements defined for step: ${step}")
        }
        return entry.requiredColumns
    }

    /**
     * The checkpoint-CSV column to read when `step` is the run's entry point
     * (--start). Replaces mirage.nf's hand-written entry_column map.
     */
    static String entryColumnForStep(String step) {
        def entry = STEPS.find { it.name == step }
        if (!entry) {
            throw new IllegalArgumentException("No entry column defined for step: ${step}")
        }
        return entry.entryColumn
    }

    /**
     * Whether `step` is this run's --start step -- i.e. whether the channel for
     * `step` must come from INPUT_CHECK.out.samples rather than the previous
     * step's direct output. Named helper so mirage.nf's routing reads its intent
     * instead of repeating the raw `params.start == '...'` comparison at every
     * branch point.
     */
    static boolean isEntryPoint(Map params, String step) {
        return params.start == step
    }

    /**
     * Whether a patient with no `is_reference=true` row may have one auto-promoted.
     *
     * TRUE ONLY AT `--start preprocessing`. At every later entry point the samplesheet
     * IS a checkpoint CSV this pipeline wrote, and a checkpoint always records exactly
     * one reference per patient (INPUT_CHECK resolves it before the first checkpoint is
     * written, so the decision is made once and then carried). A later entry point
     * therefore never NEEDS to infer -- and must not: a checkpoint arriving without its
     * reference is corrupt or hand-edited, and quietly promoting some row would register
     * against a different slide than the run that produced the checkpoint, with nothing
     * in the output saying so.
     *
     * This was real behaviour, not a hypothetical: feeding an all-false
     * `csv/preprocessed.csv` to `--start registration --allow_auto_reference true` used
     * to promote whichever slide arrived first.
     *
     * The ONE site that computes this. `params.allow_auto_reference` is the user's
     * request; this is whether the request applies. Three callers ask (mirage.nf's two
     * CsvUtils validations and its INPUT_CHECK call) and none re-derives it.
     * mode='add_cycle' passes `false` directly and never comes through here -- its
     * reference is the prior run's and is never a row in its sheet.
     */
    static boolean autoReferenceAllowed(Map params) {
        return params.allow_auto_reference && isEntryPoint(params, 'preprocessing')
    }
}
