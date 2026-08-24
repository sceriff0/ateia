/*
 * CsvUtils - helpers for reading the pipeline's input sample sheet.
 *
 * Parses the input CSV (quote-aware), extracts per-sample metadata, and derives
 * the per-patient / per-channel counts that the workflow injects into meta maps
 * so channels can stream through groupTuple without buffering every sample.
 */
class CsvUtils {

    private static List<String> parseCsvLine(String line) {
        def fields = []
        def current = new StringBuilder()
        boolean inQuotes = false
        for (int i = 0; i < line.length(); i++) {
            char c = line.charAt(i)
            if (c == '"' as char) {
                // Handle escaped quotes ("") inside quoted fields
                if (inQuotes && i + 1 < line.length() && line.charAt(i + 1) == '"' as char) {
                    current.append('"')
                    i++  // skip the second quote
                } else {
                    inQuotes = !inQuotes
                }
            } else if (c == ',' as char && !inQuotes) {
                fields << current.toString().trim()
                current = new StringBuilder()
            } else {
                current.append(c)
            }
        }
        fields << current.toString().trim()
        return fields
    }

    /**
     * Read a CSV's lines, stripping a UTF-8 byte-order mark from the header if
     * present. Excel/Windows commonly save CSVs with a leading BOM (U+FEFF),
     * which otherwise gets glued onto the first header column name
     * ("<BOM>patient_id"), making every column lookup return -1 — silently
     * breaking image/channel counting (streaming groupTuple hangs) and input
     * validation. Stripping it once here protects every reader below.
     */
    private static List<String> readCsvLines(String csvPath) {
        def lines = new File(csvPath).readLines()
        if (lines && lines[0] && ((int) lines[0].charAt(0)) == 0xFEFF)
            lines[0] = lines[0].substring(1)
        return lines
    }

    /**
     * Count images per patient from a CSV file.
     * Returns a Map of patient_id -> count
     */
    static Map<String, Integer> countImagesPerPatient(String csvPath) {
        def file = new File(csvPath)
        if (!file.exists()) return [:]

        def counts = [:].withDefault { 0 }
        def lines = readCsvLines(file.path)
        if (lines.size() < 2) return [:]  // Header only or empty

        def header = parseCsvLine(lines[0])
        def patientIdx = header.findIndexOf { it == 'patient_id' }
        if (patientIdx == -1) return [:]

        lines.drop(1).each { line ->
            def cols = parseCsvLine(line)
            if (cols.size() > patientIdx) {
                def patientId = cols[patientIdx].trim()
                if (patientId) counts[patientId]++  // ignore blank patient_id cells
            }
        }
        return counts
    }

    /**
     * Count the channels per patient that actually reach quantification.
     *
     * This is the size the postprocessing groupKeys are built from (feeds
     * meta.channels_count via input_check.nf), so it must equal the number of
     * single-channel TIFFs SPLIT_CHANNELS produces for the patient -- not the number
     * of channels the samplesheet declares. The two differ because the
     * emit-set is resolved per slide by resolveKeptChannelsPerSlide, which claims each
     * marker name exactly once per patient: the reference is walked first, then the
     * remaining slides in samplesheet order, and a slide keeps only names nothing has
     * claimed yet. A reference-less sheet declaring `DAPI|KI67|CD20` on one slide still
     * yields THREE markers; a second slide re-declaring `DAPI` adds nothing. Unioning
     * declared channels with no reference awareness (what this did before) over-counted,
     * and an over-counted groupKey never fills.
     *
     * Do NOT point run_summary.json's input manifest at this. The manifest should
     * report what the samplesheet declared, not what survives the nuclear-channel
     * drop -- that consumer is countDeclaredChannelsPerPatient below. Feeding the
     * manifest from THIS function is the exact regression closed in the branch that
     * added it: for add_cycle it silently reported 2 channels for a declared 3-channel
     * cycle. One number, one purpose; see that function's doc for the other half.
     *
     * @param csvPath        path to the samplesheet
     * @param imageColumn    the column holding the image this step consumes; passed
     *                       straight through to resolveReferenceRows so the reference
     *                       used for counting is the same row registration will use
     * @param nuclearMarkers params.nuclear_markers -- required, never defaulted here
     * @param autoReference  whether a patient with NO is_reference=true row will have
     *                       one auto-promoted from its own rows. True mirrors
     *                       params.allow_auto_reference on the linear path, where
     *                       registration.nf promotes the patient's first image and so
     *                       keeps its nuclear channel. FALSE for mode=add_cycle, whose
     *                       reference is the prior run's and never a row in this sheet.
     *
     * Returns a Map of patient_id -> channel count.
     */
    /**
     * patient_id -> the value of `imageColumn` on the row that IS that patient's
     * registration reference. THE one place the reference is decided.
     *
     * Rules, in order:
     *   1. the row declaring `is_reference=true` wins;
     *   2. otherwise, when `autoReference`, the patient's FIRST row IN SAMPLESHEET
     *      ORDER;
     *   3. otherwise the patient is absent from the returned map -- it has no
     *      reference, which is legitimate for mode='add_cycle' (whose reference is
     *      the prior run's and never a row in its sheet).
     *
     * WHY SAMPLESHEET ORDER, AND WHY HERE. This rule used to exist TWICE, resolved
     * from two different orderings of the same data:
     *
     *   countChannelsPerPatient (below)  promoted rows[0]  -- samplesheet order
     *   subworkflows/local/registration.nf  promoted items[0] -- ARRIVAL order
     *
     * The second is a `.groupTuple()` result, so it is whichever slide finished
     * preprocessing first. Two runs of the same data could therefore register against
     * different references. Worse, the first sizes `channels_count`, which sizes the
     * streaming groupKey the whole pipeline's fan-in rests on: when the two copies
     * disagree AND the two slides differ in nuclear-marker content, the group is sized
     * for a slide that is not the reference. Latent rather than observed today only
     * because the test data's two slides both carry DAPI.
     *
     * Resolving at samplesheet-read time also puts the decision UPSTREAM of the first
     * checkpoint writer, which is what lets `is_reference=true` reach
     * `csv/preprocessed.csv`. It previously did not: registration.nf promoted after
     * that file was written, so an --allow_auto_reference run emitted an all-false
     * checkpoint that its own `--start registration` then refused to read.
     *
     * THIS METHOD RESOLVES; IT DOES NOT VALIDATE. "Exactly one reference per patient"
     * and "no reference is an error unless allowed" stay in validateInputSemantics,
     * which runs first and has the better messages. Throwing here would break
     * add_cycle, whose zero-reference sheet is by design.
     *
     * @param imageColumn the column holding the image this step consumes -- the same
     *                    `entry_column` INPUT_CHECK carries forward, so both callers
     *                    key the resolution identically. Assumes that value is unique
     *                    per row within a patient: true for every checkpoint CSV
     *                    (paths are distinct) and for any sheet not listing one image
     *                    twice.
     */
    static Map<String, String> resolveReferenceRows(String csvPath, String imageColumn, boolean autoReference) {
        def file = new File(csvPath)
        if (!file.exists()) return [:]

        def lines = readCsvLines(file.path)
        if (lines.size() < 2) return [:]

        def header = parseCsvLine(lines[0])
        def patientIdx = header.findIndexOf { it == 'patient_id' }
        def imageIdx   = header.findIndexOf { it == imageColumn }
        def refIdx     = header.findIndexOf { it == 'is_reference' }
        if (patientIdx == -1 || imageIdx == -1) return [:]

        // Rows per patient, IN SAMPLESHEET ORDER -- rule 2 depends on that order, so
        // this must stay an ordered accumulation.
        def rowsByPatient = [:].withDefault { [] }
        lines.drop(1).each { line ->
            def cols = parseCsvLine(line)
            if (cols.size() <= Math.max(patientIdx, imageIdx)) return
            def patientId = cols[patientIdx].trim()
            if (!patientId) return  // ignore blank patient_id cells
            // Lenient parse, matching countChannelsPerPatient below: validateInputSemantics
            // has already rejected malformed values with a better message.
            def isRef = refIdx != -1 && refIdx < cols.size() &&
                        cols[refIdx]?.trim()?.toLowerCase() == 'true'
            rowsByPatient[patientId] << [isRef, cols[imageIdx].trim()]
        }

        def resolved = [:]
        rowsByPatient.each { patientId, rows ->
            def declared = rows.find { it[0] }
            if (declared) { resolved[patientId] = declared[1]; return }
            if (autoReference && rows) resolved[patientId] = rows[0][1]
            // else: no reference for this patient -- omitted deliberately (rule 3).
        }
        return resolved
    }

    /**
     * patient_id -> channels of the REFERENCE row, from a checkpoint CSV.
     *
     * add_cycle's seed for resolveKeptChannelsPerSlide's `preClaimed`: the prior run's
     * pyramid already contains these markers, so a new cycle re-staining one adds
     * nothing. Read synchronously rather than from ch_prior_ref, because the keep-set
     * has to be known while the workflow is being constructed, not when a channel
     * happens to emit.
     */
    static Map<String, List<String>> referenceChannelsPerPatient(String csvPath) {
        def file = new File(csvPath)
        if (!file.exists()) return [:]

        def lines = readCsvLines(file.path)
        if (lines.size() < 2) return [:]

        def header      = parseCsvLine(lines[0])
        def patientIdx  = header.findIndexOf { it == 'patient_id' }
        def channelsIdx = header.findIndexOf { it == 'channels' }
        def refIdx      = header.findIndexOf { it == 'is_reference' }
        if (patientIdx == -1 || channelsIdx == -1 || refIdx == -1) return [:]

        def result = [:]
        lines.drop(1).each { line ->
            def cols = parseCsvLine(line)
            if (cols.size() <= Math.max(patientIdx, Math.max(channelsIdx, refIdx))) return
            if (cols[refIdx]?.trim()?.toLowerCase() != 'true') return
            def patientId = cols[patientIdx].trim()
            if (!patientId) return
            result[patientId] = cols[channelsIdx].split('\\|')*.trim().findAll { it }
        }
        return result
    }

    /**
     * Each slide's exact emit-set: patient_id -> (RAW image cell -> channels).
     *
     * THE INNER KEY IS THE RAW `<imageColumn>` CELL, verbatim (trimmed), NOT its
     * basename. resolveReferenceRows returns that same raw cell and input_check.nf's
     * meta.is_reference already compares against it, so both lookups provably use one
     * key. A basename key silently weakened resolveReferenceRows' "the raw cell is
     * unique per patient" assumption to "the FILENAME is unique per patient": two rows
     * of one patient under different directories (a cyclic-IF cohort with one directory
     * per cycle) then overwrote each other, leaving one entry that both rows read --
     * the reference got the other slide's keep-set and emitted zero channels.
     *
     * THE keep-set rule, and the only place it exists. SPLIT_CHANNELS emits exactly what
     * this returns (via meta.keep_channels); countChannelsPerPatient sizes the
     * postprocessing groupKeys from it. Before this method the same rule lived in THREE
     * places -- MarkerUtils.splitOutputChannels, bin/split_multichannel.py, and
     * SPLIT_CHANNELS' stub block -- and a disagreement between them was a silent
     * groupTuple miscount rather than a crash.
     *
     * A channel is kept iff its upper-cased name has not already been claimed by an
     * earlier slide of the same patient, walking REFERENCE FIRST then samplesheet order.
     * Ordering the reference first is what makes "the reference wins" fall out of the
     * walk instead of needing a special case.
     *
     * NUCLEAR-NESS PLAYS NO PART IN THE DROP DECISION, deliberately, and the reason is
     * DETERMINISM, not a consumer that needs a file count.
     *
     * Claiming every kept name -- nuclear or not -- makes each marker name reach
     * SPLIT_CHANNELS exactly once per patient, and the winner is the reference, else the
     * samplesheet-order-first slide. Two slides sharing a marker used to be deduplicated
     * by ARRIVAL ORDER at a downstream `.unique()`, the exact scheduling-nondeterminism
     * add_cycle.nf warns about in its own dedup: which slide's copy reached
     * merged_quant.csv and the pyramid varied run to run. It also makes channels_count
     * (countChannelsPerPatient, below) EXACT against what actually arrives, because the
     * emitted-FILE count and the DISTINCT-NAME count are then the same number and the
     * sized groupKey is right for either consumer without knowing which it is.
     *
     * BE ACCURATE ABOUT WHY, because an earlier version of this comment was not: it said
     * groupTiffsByPatient "has no `.unique` and needs the FILE count". The FUNCTION has
     * no `.unique`, but BOTH of its callers dedup on [patient_id, marker] immediately
     * upstream of it -- subworkflows/local/postprocess.nf's `.unique { ... [patient_id,
     * marker] }` and subworkflows/local/add_cycle.nf's priority groupTuple on
     * [pid, marker]. So no live consumer needs the file count today, and the
     * under-count/ABORT scenario that claim cited is unreachable. The decision stands on
     * determinism and on one clean invariant; do not restate the unreachable one.
     *
     * `preClaimed` seeds the claimed set per patient. add_cycle passes the prior run's
     * reference channels, so a re-stained DAPI is dropped as redundant while a genuinely
     * new nuclear marker survives.
     *
     * @param nuclearMarkers validated via MarkerUtils.markerList so a malformed
     *        params.nuclear_markers still fails loudly here, even though the keep
     *        decision itself no longer branches on it.
     */
    static Map<String, Map<String, List<String>>> resolveKeptChannelsPerSlide(
            String csvPath, String imageColumn, def nuclearMarkers, boolean autoReference,
            Map<String, List<String>> preClaimed = [:]) {

        // Validate the parameter shape even though the keep rule does not branch on it: a
        // malformed params.nuclear_markers must not become silently harmless here.
        MarkerUtils.markerList(nuclearMarkers)

        def file = new File(csvPath)
        if (!file.exists()) return [:]

        def lines = readCsvLines(file.path)
        if (lines.size() < 2) return [:]

        def header      = parseCsvLine(lines[0])
        def patientIdx  = header.findIndexOf { it == 'patient_id' }
        def channelsIdx = header.findIndexOf { it == 'channels' }
        def imageIdx    = header.findIndexOf { it == imageColumn }
        if (patientIdx == -1 || channelsIdx == -1 || imageIdx == -1) return [:]

        // WHICH slide is the reference is resolved by resolveReferenceRows, not decided
        // here -- the same reason countChannelsPerPatient asks it rather than promoting
        // rows[0] itself.
        def referenceImage = resolveReferenceRows(csvPath, imageColumn, autoReference)

        // Rows per patient, IN SAMPLESHEET ORDER. The walk order below depends on it.
        def rowsByPatient = [:].withDefault { [] }
        lines.drop(1).each { line ->
            def cols = parseCsvLine(line)
            if (cols.size() <= Math.max(patientIdx, Math.max(channelsIdx, imageIdx))) return
            def patientId = cols[patientIdx].trim()
            if (!patientId) return  // ignore blank patient_id cells
            def rawImage = cols[imageIdx].trim()
            rowsByPatient[patientId] << [
                raw     : rawImage,
                channels: cols[channelsIdx].split('\\|')*.trim().findAll { it },
            ]
        }

        def result = [:]
        rowsByPatient.each { patientId, rows ->
            // resolveReferenceRows returns the RAW cell, which is also this map's key.
            def refCell = referenceImage[patientId]
            // Stable partition: reference row(s) first, everything else in declared
            // order. A patient with no reference at all (add_cycle's by-design
            // zero-reference sheet) simply walks in samplesheet order, since nothing
            // matches null.
            def ordered = rows.findAll { it.raw == refCell } +
                          rows.findAll { it.raw != refCell }

            def claimed = new HashSet<String>()
            (preClaimed[patientId] ?: []).each { claimed << it.toString().trim().toUpperCase() }

            def perSlide = [:]
            ordered.each { row ->
                def keep = []
                row.channels.each { ch ->
                    def name = ch.toUpperCase()
                    if (claimed.contains(name)) return
                    claimed << name
                    keep << ch
                }
                perSlide[row.raw] = keep
            }
            result[patientId] = perSlide
        }
        return result
    }

    static Map<String, Integer> countChannelsPerPatient(String csvPath, String imageColumn, def nuclearMarkers, boolean autoReference) {
        // Every guard the old body carried inline -- missing file, header-only sheet,
        // absent patient_id/channels/<imageColumn> column -- now lives in the resolver,
        // which returns an empty map in each case. Reference resolution likewise: it
        // asks resolveReferenceRows, so this method and SPLIT_CHANNELS cannot disagree
        // about which slide is the reference.
        //
        // Derived from resolveKeptChannelsPerSlide, which IS the rule SPLIT_CHANNELS
        // applies (it emits exactly meta.keep_channels). Summing the per-slide list
        // sizes is safe precisely because that resolver claims each marker name once
        // per patient: the sum therefore equals BOTH the number of TIFFs emitted and the
        // number of distinct names, because those are the same number. Every
        // channels_count-sized groupKey downstream is then correct without its consumer
        // having to know which of the two it is being handed.
        //
        // NOT because some consumer needs the file count: both groupTiffsByPatient
        // callers dedup on [patient_id, marker] immediately upstream of it
        // (subworkflows/local/postprocess.nf's `.unique`, subworkflows/local/add_cycle.nf's
        // priority groupTuple), so the distinct-name count would in fact serve them
        // today. An earlier version of this comment claimed otherwise -- see
        // resolveKeptChannelsPerSlide's doc.
        //
        // This used to union upper-cased names into a HashSet, which gave the
        // distinct-name count but NOT the file count -- correct only while no two slides
        // could emit the same marker.
        def kept = resolveKeptChannelsPerSlide(csvPath, imageColumn, nuclearMarkers, autoReference)
        return kept.collectEntries { patientId, perSlide ->
            [patientId, (perSlide.values().sum { it.size() } ?: 0)]
        }
    }

    /**
     * Count the DECLARED channels per patient: the union of the samplesheet's
     * `channels` column values, patient-wide -- no reference awareness, no
     * nuclear-marker awareness, no extra arguments. This is deliberately
     * countChannelsPerPatient's exact pre-Task-3 behaviour, kept alive for a
     * different consumer: run_summary.json's input manifest
     * (`manifest.totals.channels` / `manifest.patients[pid].channels`), which should
     * report what the samplesheet SAID, not what reaches QUANTIFY.
     *
     * Do NOT feed this into channels_count / the groupKey size. That reintroduces
     * the exact bug countChannelsPerPatient's exactness fixed: unioning declared
     * channels with no reference awareness over-counts a reference-less sheet by its
     * dropped nuclear channel, and an over-counted groupKey never fills (the run
     * hangs). See countChannelsPerPatient's doc for that consumer's requirements.
     *
     * @param csvPath path to the samplesheet
     *
     * Returns a Map of patient_id -> declared channel count.
     */
    static Map<String, Integer> countDeclaredChannelsPerPatient(String csvPath) {
        def file = new File(csvPath)
        if (!file.exists()) return [:]

        def channelSets = [:].withDefault { new HashSet<String>() }
        def lines = readCsvLines(file.path)
        if (lines.size() < 2) return [:]  // Header only or empty

        def header = parseCsvLine(lines[0])
        def patientIdx = header.findIndexOf { it == 'patient_id' }
        def channelsIdx = header.findIndexOf { it == 'channels' }
        if (patientIdx == -1 || channelsIdx == -1) return [:]

        lines.drop(1).each { line ->
            def cols = parseCsvLine(line)
            if (cols.size() > Math.max(patientIdx, channelsIdx)) {
                def patientId = cols[patientIdx].trim()
                if (!patientId) return  // ignore blank patient_id cells
                def channels = cols[channelsIdx].split('\\|')*.trim().findAll { it }
                channelSets[patientId].addAll(channels*.toUpperCase())
            }
        }

        return channelSets.collectEntries { k, v -> [k, v.size()] }
    }

    static Map validateMetadata(Map meta, def nuclearMarkers, String context = 'unknown') {

        if (!meta.patient_id)
            throw new IllegalArgumentException("Missing patient_id in ${context}")

        if (!(meta.is_reference instanceof Boolean))
            throw new IllegalArgumentException("is_reference must be boolean in ${context}")

        if (!(meta.channels instanceof List) || meta.channels.isEmpty())
            throw new IllegalArgumentException("channels must be a non-empty List in ${context}")

        if (meta.channels.any { it == null || it.trim().isEmpty() })
            throw new IllegalArgumentException("Empty channel name found for patient ${meta.patient_id}")

        // The nuclear/fiducial marker may appear at ANY position (segmentation and the
        // registration fiducial locate it by name, not index). Only its presence is
        // required, and WHICH names qualify comes from params.nuclear_markers via
        // MarkerUtils — not a hardcoded 'DAPI', which rejected an otherwise valid
        // CELLTOX-only samplesheet before the run could start.
        if (!MarkerUtils.hasNuclear(meta.channels, nuclearMarkers)) {
            throw new IllegalStateException("No nuclear channel (${MarkerUtils.markerList(nuclearMarkers).join(', ')}) found for patient ${meta.patient_id} (${context}). Found channels: ${meta.channels}")
        }

        return meta
    }

    /**
     * Strictly parse an is_reference cell. Accepts only 'true'/'false'
     * (case-insensitive); anything else is rejected so typos like "yes"
     * cannot be silently coerced to false and corrupt reference selection.
     */
    static Boolean parseIsReference(def value, String context = 'unknown') {
        def s = (value ?: '').toString().trim().toLowerCase()
        if (s == 'true')  return true
        if (s == 'false') return false
        throw new IllegalArgumentException("Invalid is_reference value '${value}' in ${context}. Must be 'true' or 'false'.")
    }

    static Map parseMetadata(Map row, def nuclearMarkers, String context = 'parseMetadata') {

        def channels = row.channels
            ?.split('\\|')
            ?.collect { it.trim() } ?: []

        def meta = [
            patient_id  : row.patient_id?.toString()?.trim(),
            is_reference: parseIsReference(row.is_reference, "${context} (${row.patient_id})"),
            channels    : channels
        ]

        return validateMetadata(meta, nuclearMarkers, "${context} (${row.patient_id})")
    }

    static void validateInputCSV(def csv, List required_cols) {

        def file = new File(csv)
        if (!file.exists())
            throw new FileNotFoundException("Input CSV not found: ${csv}")

        def lines = readCsvLines(file.path)
        if (lines.isEmpty())
            throw new RuntimeException("CSV is empty: ${csv}")

        def header = parseCsvLine(lines.first())

        required_cols.each {
            if (!(it in header))
                throw new IllegalArgumentException("Missing required column '${it}' in CSV: ${csv}")
        }
    }

    /**
     * Fail-fast semantic validation of the whole samplesheet, run at parse
     * time (and therefore visible under --dry_run). Complements the per-row
     * checks that otherwise only fire later during channel construction.
     *
     * Validates, for every data row: is_reference format, channel list /
     * nuclear-marker presence, and existence of the step's image file. Validates, per
     * patient: exactly one reference image (zero allowed only when
     * allow_auto_reference is set; more than one is always ambiguous).
     *
     * @param csv                  path to the input samplesheet
     * @param step                 pipeline start step (selects the path column)
     * @param allowAutoReference   whether a patient may omit an explicit reference
     * @param nuclearMarkers       params.nuclear_markers — required, never defaulted here
     */
    static void validateInputSemantics(def csv, String step, boolean allowAutoReference, def nuclearMarkers) {

        // ParamUtils.STEPS is the single source of truth for "what is a step?"
        // (name / requiredColumns / entryColumn / qcKinds) -- see its header
        // comment in lib/ParamUtils.groovy. entryColumnForStep throws on an
        // unrecognised step rather than the old map literal's silent `null`,
        // but both call sites below only ever pass a step already validated
        // by nextflow_schema.json's enum (or the add_cycle branch's hardcoded
        // 'preprocessing'), so that stricter failure mode is unreachable in
        // practice and loud instead of silent if it ever is reached.
        def pathColumn = ParamUtils.entryColumnForStep(step)

        def lines = readCsvLines(csv)
        if (lines.size() < 2)
            throw new IllegalStateException("Input CSV contains no data rows: ${csv}")

        def header     = parseCsvLine(lines[0])
        def piIdx      = header.findIndexOf { it == 'patient_id' }
        def refIdx     = header.findIndexOf { it == 'is_reference' }
        def chIdx      = header.findIndexOf { it == 'channels' }
        def pathIdx    = pathColumn ? header.findIndexOf { it == pathColumn } : -1

        def refCounts = [:].withDefault { 0 }
        def rowCounts = [:].withDefault { 0 }

        lines.drop(1).eachWithIndex { line, i ->
            def cols = parseCsvLine(line)
            if (cols.every { it == null || it.trim().isEmpty() }) return  // skip blank lines

            def ctx = "row ${i + 2} of ${csv}"
            def row = [
                patient_id  : piIdx  >= 0 ? cols[piIdx]?.trim() : null,
                is_reference: refIdx >= 0 ? cols[refIdx] : null,
                channels    : chIdx  >= 0 && chIdx < cols.size() ? cols[chIdx] : null,
            ]

            // Per-row format + nuclear-channel validation (throws on problems).
            def parsed = parseMetadata(row, nuclearMarkers, ctx)

            // Image file must exist (resolved against the launch directory for
            // relative paths). Skipped only if the path column is absent.
            if (pathIdx >= 0 && pathIdx < cols.size()) {
                def p = cols[pathIdx].trim()
                if (!p)
                    throw new IllegalArgumentException("Empty path in column '${pathColumn}' for patient ${row.patient_id} (${ctx})")
                if (!new File(p).exists())
                    throw new FileNotFoundException("Input file does not exist: ${p} (patient ${row.patient_id}, ${ctx})")
            }

            rowCounts[row.patient_id]++
            if (parsed.is_reference) refCounts[row.patient_id]++  // reuse parsed value (no re-parse)
        }

        rowCounts.each { patientId, _n ->
            def refs = refCounts[patientId]
            if (refs > 1)
                throw new IllegalStateException("Multiple reference images found for patient ${patientId} (${refs} found). Exactly one image per patient may set is_reference=true.")
            if (refs == 0 && !allowAutoReference)
                throw new IllegalStateException("No reference image found for patient ${patientId}. Set is_reference=true for one image, or run with --allow_auto_reference true (which applies at --start preprocessing ONLY -- at a later entry point the samplesheet is a checkpoint this pipeline wrote and must already name its reference; see ParamUtils.autoReferenceAllowed).")
        }
    }
}