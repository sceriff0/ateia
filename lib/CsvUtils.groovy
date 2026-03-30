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
        
        // Check if DAPI exists anywhere, not just at index 0
        if (!meta.channels.any { it.toUpperCase() == 'DAPI' }) {
            throw new IllegalStateException("""
                DAPI channel missing for patient ${meta.patient_id}
                Context: ${context}
                Found channels: ${meta.channels}
                """.stripIndent())
        }

        return meta
    }

    static Map parseMetadata(Map row, String context = 'parseMetadata') {

        def channels = row.channels
            ?.split('\\|')
            ?.collect { it.trim() } ?: []

        def meta = [
            patient_id  : row.patient_id,
            is_reference: row.is_reference?.toBoolean(),
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
                throw new NoSuchFieldException("CSV missing required column '${it}'")
        }
    }
}