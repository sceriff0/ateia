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
     * Count images per patient from a CSV file.
     * Returns a Map of patient_id -> count
     */
    static Map<String, Integer> countImagesPerPatient(String csvPath) {
        def file = new File(csvPath)
        if (!file.exists()) return [:]

        def counts = [:].withDefault { 0 }
        def lines = file.readLines()
        if (lines.size() < 2) return [:]  // Header only or empty

        def header = parseCsvLine(lines[0])
        def patientIdx = header.findIndexOf { it == 'patient_id' }
        if (patientIdx == -1) return [:]

        lines.drop(1).each { line ->
            def cols = parseCsvLine(line)
            if (cols.size() > patientIdx) {
                def patientId = cols[patientIdx].trim()
                counts[patientId]++
            }
        }
        return counts
    }

    /**
     * Count unique channels per patient from a CSV file.
     * Returns a Map of patient_id -> unique channel count
     * Used for streaming groupTuple with groupKey in postprocessing.
     */
    static Map<String, Integer> countChannelsPerPatient(String csvPath) {
        def file = new File(csvPath)
        if (!file.exists()) return [:]

        def channelSets = [:].withDefault { new HashSet<String>() }
        def lines = file.readLines()
        if (lines.size() < 2) return [:]  // Header only or empty

        def header = parseCsvLine(lines[0])
        def patientIdx = header.findIndexOf { it == 'patient_id' }
        def channelsIdx = header.findIndexOf { it == 'channels' }
        if (patientIdx == -1 || channelsIdx == -1) return [:]

        lines.drop(1).each { line ->
            def cols = parseCsvLine(line)
            if (cols.size() > Math.max(patientIdx, channelsIdx)) {
                def patientId = cols[patientIdx].trim()
                def channels = cols[channelsIdx].split('\\|')*.trim().findAll { it }
                channelSets[patientId].addAll(channels*.toUpperCase())
            }
        }

        // Convert Set sizes to counts
        return channelSets.collectEntries { k, v -> [k, v.size()] }
    }

    static Map validateMetadata(Map meta, String context = 'unknown') {

        if (!meta.patient_id)
            throw new IllegalArgumentException("Missing patient_id in ${context}")

        if (!(meta.is_reference instanceof Boolean))
            throw new IllegalArgumentException("is_reference must be boolean in ${context}")

        if (!(meta.channels instanceof List) || meta.channels.isEmpty())
            throw new IllegalArgumentException("channels must be a non-empty List in ${context}")

        if (meta.channels.any { it == null || it.trim().isEmpty() })
            throw new IllegalArgumentException("Empty channel name found for patient ${meta.patient_id}")

        // DAPI may appear at ANY position (segmentation locates it by name, not index).
        // Only its presence is required.
        if (!meta.channels.any { it.toUpperCase() == 'DAPI' }) {
            throw new IllegalStateException("DAPI channel not found for patient ${meta.patient_id} (${context}). Found channels: ${meta.channels}")
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

    static Map parseMetadata(Map row, String context = 'parseMetadata') {

        def channels = row.channels
            ?.split('\\|')
            ?.collect { it.trim() } ?: []

        def meta = [
            patient_id  : row.patient_id,
            is_reference: parseIsReference(row.is_reference, "${context} (${row.patient_id})"),
            channels    : channels
        ]

        return validateMetadata(meta, "${context} (${row.patient_id})")
    }

    static void validateInputCSV(def csv, List required_cols) {

        def file = new File(csv)
        if (!file.exists())
            throw new FileNotFoundException("Input CSV not found: ${csv}")

        def lines = file.readLines()
        if (lines.isEmpty())
            throw new RuntimeException("CSV is empty: ${csv}")

        def header = parseCsvLine(lines.first())

        required_cols.each {
            if (!(it in header))
                throw new NoSuchFieldException("Missing required column '${it}' in CSV: ${csv}")
        }
    }

    /**
     * Fail-fast semantic validation of the whole samplesheet, run at parse
     * time (and therefore visible under --dry_run). Complements the per-row
     * checks that otherwise only fire later during channel construction.
     *
     * Validates, for every data row: is_reference format, channel list /
     * DAPI presence, and existence of the step's image file. Validates, per
     * patient: exactly one reference image (zero allowed only when
     * allow_auto_reference is set; more than one is always ambiguous).
     *
     * @param csv                  path to the input samplesheet
     * @param step                 pipeline start step (selects the path column)
     * @param allowAutoReference   whether a patient may omit an explicit reference
     */
    static void validateInputSemantics(def csv, String step, boolean allowAutoReference) {

        def pathColumn = [
            preprocessing : 'path_to_file',
            registration  : 'preprocessed_image',
            postprocessing: 'registered_image',
        ][step]

        def lines = new File(csv).readLines()
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
                patient_id  : piIdx  >= 0 ? cols[piIdx]  : null,
                is_reference: refIdx >= 0 ? cols[refIdx] : null,
                channels    : chIdx  >= 0 && chIdx < cols.size() ? cols[chIdx] : null,
            ]

            // Per-row format + DAPI/channel validation (throws on problems).
            parseMetadata(row, ctx)

            // Image file must exist (resolved against the launch directory for
            // relative paths). Skipped only if the path column is absent.
            if (pathIdx >= 0 && pathIdx < cols.size()) {
                def p = cols[pathIdx].trim()
                if (p && !new File(p).exists())
                    throw new FileNotFoundException("Input file does not exist: ${p} (patient ${row.patient_id}, ${ctx})")
            }

            rowCounts[row.patient_id]++
            if (parseIsReference(row.is_reference, ctx)) refCounts[row.patient_id]++
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