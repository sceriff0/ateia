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
     * nuclear/fiducial channel is kept only on the reference slide (see
     * MarkerUtils.splitOutputChannels): a reference-less sheet declaring
     * `DAPI|KI67|CD20` yields TWO markers, not three. Unioning declared channels with
     * no reference awareness (what this did before) over-counted exactly that case,
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
    static Map<String, Integer> countChannelsPerPatient(String csvPath, def nuclearMarkers, boolean autoReference) {
        def file = new File(csvPath)
        if (!file.exists()) return [:]

        def lines = readCsvLines(file.path)
        if (lines.size() < 2) return [:]  // Header only or empty

        def header = parseCsvLine(lines[0])
        def patientIdx = header.findIndexOf { it == 'patient_id' }
        def channelsIdx = header.findIndexOf { it == 'channels' }
        def refIdx = header.findIndexOf { it == 'is_reference' }
        if (patientIdx == -1 || channelsIdx == -1) return [:]

        // Collect the rows per patient first: whether a patient has an explicit
        // reference is only knowable after every row has been read.
        def rowsByPatient = [:].withDefault { [] }
        lines.drop(1).each { line ->
            def cols = parseCsvLine(line)
            if (cols.size() > Math.max(patientIdx, channelsIdx)) {
                def patientId = cols[patientIdx].trim()
                if (!patientId) return  // ignore blank patient_id cells
                // Lenient parse: validateInputSemantics has already rejected malformed
                // values with a better message by the time counting runs. A missing
                // is_reference column degrades to "every row is a reference", which
                // over-counts rather than under-counts -- the safe direction.
                def isRef = refIdx == -1 ? true
                    : (refIdx < cols.size() && cols[refIdx]?.trim()?.toLowerCase() == 'true')
                def channels = cols[channelsIdx].split('\\|')*.trim().findAll { it }
                rowsByPatient[patientId] << [isRef, channels]
            }
        }

        return rowsByPatient.collectEntries { patientId, rows ->
            def effective = rows
            if (autoReference && !rows.any { it[0] }) {
                // registration.nf promotes the patient's FIRST image to reference, which
                // keeps that slide's nuclear channel in the split output.
                effective = [[true, rows[0][1]]] + rows.drop(1)
            }
            def kept = new HashSet<String>()
            effective.each { isRef, channels ->
                kept.addAll(MarkerUtils.splitOutputChannels(channels, isRef, nuclearMarkers)*.toUpperCase())
            }
            [patientId, kept.size()]
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
                throw new IllegalStateException("No reference image found for patient ${patientId}. Set is_reference=true for one image, or run with --allow_auto_reference true.")
        }
    }
}