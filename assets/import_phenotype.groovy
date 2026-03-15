/**
 * ============================================================================
 * QUPATH SCRIPT: Import Phenotypes (N-Layer with Keyboard Toggle)
 * ============================================================================
 *
 * Imports cells from GeoJSON with phenotype classifications and colors.
 * Supports N layers: import phenotypes, pixie clusters, and more.
 *
 * FEATURES:
 *   - Import & Merge: Add new classification layers to existing detections
 *   - Keyboard Toggle: Shift+1/2/3... instantly hides/shows layers
 *   - N-layer support: Combine unlimited classification sources
 *
 * KEYBOARD SHORTCUTS (active after running this script):
 *   Shift+1  → Toggle layer at position 0 (e.g., phenotypes)
 *   Shift+2  → Toggle layer at position 1 (e.g., pixie_clusters)
 *   Shift+3  → Toggle layer at position 2
 *   ...up to Shift+9
 *
 * USAGE:
 *   1. Open your pyramidal OME-TIFF in QuPath
 *   2. Automate → Show script editor → Paste this script
 *   3. Run (Ctrl+R) → Select your .geojson file
 *   4. Use Shift+1/2/3 to toggle layers on/off instantly
 *   5. Run again to merge another layer
 *
 * ============================================================================
 */

import qupath.lib.objects.PathObjects
import qupath.lib.objects.classes.PathClass
import qupath.lib.roi.ROIs
import qupath.lib.regions.ImagePlane
import qupath.lib.gui.dialogs.Dialogs
import com.google.gson.JsonParser
import javafx.scene.input.KeyCode
import javafx.scene.input.KeyEvent

// ============================================================================
// CONFIGURATION
// ============================================================================

def CELL_RADIUS_UM = 3.0  // Cell marker radius in micrometers

// ============================================================================
// HELPER FUNCTIONS
// ============================================================================

def phenotypeColors = [:]

def getPathClass = { String name ->
    def rgb = phenotypeColors[name]
    if (rgb) {
        return PathClass.fromString(name, getColorRGB(rgb[0], rgb[1], rgb[2]))
    }
    def hash = Math.abs(name.hashCode())
    def r = ((hash >> 16) & 0xFF)
    def g = ((hash >> 8) & 0xFF)
    def b = (hash & 0xFF)
    if (r + g + b < 200) {
        r = Math.min(255, r + 80)
        g = Math.min(255, g + 80)
        b = Math.min(255, b + 80)
    }
    return PathClass.fromString(name, getColorRGB(r, g, b))
}

// Discover all layers with their position and visibility state
def discoverLayerStates = {
    def layers = [:]
    def detections = getDetectionObjects()
    if (detections.size() == 0) return layers

    def sampleSize = Math.min(200, detections.size())
    for (int s = 0; s < sampleSize; s++) {
        def det = detections[s]
        def ml = det.getMeasurementList()
        for (int i = 0; i < ml.size(); i++) {
            def mName = ml.getMeasurementName(i)
            if (mName.endsWith("] _position")) {
                def match = (mName =~ /^\[(.+?)\] _position$/)
                if (match.find()) {
                    def layerName = match.group(1)
                    def pos = Math.round(ml.getMeasurementValue(i)) as int
                    if (!layers.containsKey(layerName)) {
                        layers[layerName] = [position: pos, hidden: false]
                    }
                }
            }
            if (mName.endsWith("] _hidden")) {
                def match = (mName =~ /^\[(.+?)\] _hidden$/)
                if (match.find()) {
                    def layerName = match.group(1)
                    def val = ml.getMeasurementValue(i)
                    if (layers.containsKey(layerName) && val > 0) {
                        layers[layerName].hidden = true
                    }
                }
            }
        }
    }
    return layers
}

// Get sorted layer names (by position)
def discoverLayers = {
    def states = discoverLayerStates()
    return states.sort { it.value.position }.collect { it.key }
}

// Generate unique layer name
def uniqueLayerName = { String baseName, List existingLayers ->
    if (!existingLayers.contains(baseName)) return baseName
    def suffix = 2
    while (existingLayers.contains("${baseName}_${suffix}")) {
        suffix++
    }
    return "${baseName}_${suffix}"
}

// Rebuild classification label from visible layers (reads _class_id measurements)
def rebuildClassLabel = { det, layerStates, imageProps ->
    def parts = []
    layerStates.sort { it.value.position }.each { layerName, info ->
        if (info.hidden) return

        def classIdKey = "[${layerName}] _class_id"
        def ml = det.getMeasurementList()
        def classId = null
        for (int i = 0; i < ml.size(); i++) {
            if (ml.getMeasurementName(i) == classIdKey) {
                classId = Math.round(ml.getMeasurementValue(i)) as int
                break
            }
        }

        if (classId != null && classId >= 0) {
            def mappingStr = imageProps.get("_layer_classes_${layerName}")
            if (mappingStr != null) {
                def classNames = mappingStr.split("\\|\\|")
                if (classId < classNames.length) {
                    parts.add(classNames[classId])
                }
            }
        }
    }
    return parts.size() > 0 ? parts.join(" | ") : "Unclassified"
}

// ============================================================================
// FAST IN-PLACE TOGGLE (used by keyboard handler and toggle mode)
// ============================================================================

def toggleLayerByPosition = { int targetPos ->
    def layerStates = discoverLayerStates()
    if (layerStates.size() == 0) return

    // Find layer at target position
    def targetEntry = layerStates.find { it.value.position == targetPos }
    if (targetEntry == null) return

    def targetLayer = targetEntry.key
    def isHidden = targetEntry.value.hidden
    def newHidden = !isHidden
    def action = newHidden ? "Hiding" : "Showing"

    println "${action} layer '${targetLayer}' (Shift+${targetPos + 1})"

    def imgData = getCurrentImageData()
    def imageProps = imgData.getProperties()

    // Ensure class name mapping exists (first hide needs to build it)
    def mappingKey = "_layer_classes_${targetLayer}"
    if (imageProps.get(mappingKey) == null) {
        // Build mapping from current classifications
        def position = targetEntry.value.position
        def classNames = [] as LinkedHashSet
        def allDets = getDetectionObjects()

        allDets.each { det ->
            def currentClass = det.getPathClass()?.getName() ?: ""
            def parts = currentClass.split("\\s*\\|\\s*")
            // Find visible index for this position
            def visibleIndex = 0
            for (def e : layerStates.sort { it.value.position }) {
                if (e.value.position == position) break
                if (!e.value.hidden) visibleIndex++
            }
            if (visibleIndex < parts.length) {
                classNames.add(parts[visibleIndex])
            }
        }

        def mapping = classNames.toList()
        imageProps.put(mappingKey, mapping.join("||"))

        // Store _class_id in each detection
        def classIdMap = [:]
        mapping.eachWithIndex { name, idx -> classIdMap[name] = idx }
        def prefix = "[${targetLayer}] "

        allDets.each { det ->
            def currentClass = det.getPathClass()?.getName() ?: ""
            def parts = currentClass.split("\\s*\\|\\s*")
            def visibleIndex = 0
            for (def e : layerStates.sort { it.value.position }) {
                if (e.value.position == position) break
                if (!e.value.hidden) visibleIndex++
            }
            def className = (visibleIndex < parts.length) ? parts[visibleIndex] : "Unclassified"
            def classId = classIdMap.containsKey(className) ? classIdMap[className] : -1
            det.getMeasurementList().put("${prefix}_class_id", (double) classId)
        }
    }

    // Build new layer states with the toggle applied
    def newStates = layerStates.collectEntries { name, info ->
        if (name == targetLayer) {
            [name, [position: info.position, hidden: newHidden]]
        } else {
            [name, info]
        }
    }

    // Fast in-place update: setPathClass + update _hidden measurement
    def hiddenKey = "[${targetLayer}] _hidden"
    def detections = getDetectionObjects()
    detections.each { det ->
        def newLabel = rebuildClassLabel(det, newStates, imageProps)

        // Generate color from label
        def labelHash = Math.abs(newLabel.hashCode())
        def r = ((labelHash >> 16) & 0xFF)
        def g = ((labelHash >> 8) & 0xFF)
        def b = (labelHash & 0xFF)
        if (r + g + b < 200) { r = Math.min(255, r + 80); g = Math.min(255, g + 80); b = Math.min(255, b + 80) }

        det.setPathClass(PathClass.fromString(newLabel, getColorRGB(r, g, b)))
        det.getMeasurementList().put(hiddenKey, newHidden ? 1.0 : 0.0)
    }

    fireHierarchyUpdate()

    def stateWord = newHidden ? "hidden" : "visible"
    println "  '${targetLayer}' is now ${stateWord} (${detections.size()} cells updated)"
}

// ============================================================================
// REGISTER KEYBOARD SHORTCUTS
// ============================================================================

def registerKeyboardToggle = {
    def viewer = getCurrentViewer()
    if (viewer == null) return

    def view = viewer.getView()

    // Remove any existing handler from a previous script run
    def existingHandler = view.getProperties().get("_layerToggleHandler")
    if (existingHandler != null) {
        view.removeEventHandler(KeyEvent.KEY_PRESSED, existingHandler)
        view.getProperties().remove("_layerToggleHandler")
    }

    // Map Shift+1..9 to layer positions 0..8
    def digitCodes = [
        (KeyCode.DIGIT1): 0, (KeyCode.DIGIT2): 1, (KeyCode.DIGIT3): 2,
        (KeyCode.DIGIT4): 3, (KeyCode.DIGIT5): 4, (KeyCode.DIGIT6): 5,
        (KeyCode.DIGIT7): 6, (KeyCode.DIGIT8): 7, (KeyCode.DIGIT9): 8
    ]

    def handler = { KeyEvent event ->
        if (event.isShiftDown() && digitCodes.containsKey(event.getCode())) {
            def pos = digitCodes[event.getCode()]
            event.consume()
            toggleLayerByPosition(pos)
        }
    } as javafx.event.EventHandler

    view.addEventHandler(KeyEvent.KEY_PRESSED, handler)
    view.getProperties().put("_layerToggleHandler", handler)

    def layerStates = discoverLayerStates()
    if (layerStates.size() > 0) {
        println "\nKeyboard shortcuts registered:"
        layerStates.sort { it.value.position }.each { name, info ->
            def key = "Shift+${info.position + 1}"
            def state = info.hidden ? "hidden" : "visible"
            println "  ${key} → toggle '${name}' (currently ${state})"
        }
    }
}

// ============================================================================
// SCRIPT
// ============================================================================

println "=" * 60
println "QuPath Phenotype Import (N-Layer)"
println "=" * 60

// Check image
def imageData = getCurrentImageData()
if (imageData == null) {
    Dialogs.showErrorMessage("Error", "Please open an image first!")
    return
}

def server = imageData.getServer()
def pixelSize = server.getPixelCalibration().getAveragedPixelSizeMicrons()
println "Image: ${server.getMetadata().getName()}"
println "Pixel size: ${pixelSize} µm"

// Check existing detections and choose mode
def existingDetections = getDetectionObjects()
def mode = "import_fresh"

if (existingDetections.size() > 0) {
    def layerStates = discoverLayerStates()
    def layerInfo = ""
    if (layerStates.size() > 0) {
        def layerDescs = layerStates.sort { it.value.position }.collect { name, info ->
            def state = info.hidden ? "hidden" : "visible"
            def key = "Shift+${info.position + 1}"
            "  ${name} (${state}) [${key}]"
        }
        layerInfo = "\nCurrent layers:\n${layerDescs.join('\n')}"
    }

    def choice = Dialogs.showChoiceDialog(
        "Choose action",
        "Found ${existingDetections.size()} existing detections.${layerInfo}\n\n" +
        "What would you like to do?",
        ["Import & Merge new layer", "Toggle layer visibility", "Clear all & import fresh"] as String[],
        "Import & Merge new layer"
    )

    if (choice == null) {
        println "Cancelled."
        registerKeyboardToggle()
        return
    }

    switch (choice) {
        case "Import & Merge new layer":
            mode = "merge"
            break
        case "Toggle layer visibility":
            mode = "toggle"
            break
        case "Clear all & import fresh":
            mode = "clear_and_import"
            break
    }
}

println "Mode: ${mode}"

// ============================================================================
// TOGGLE LAYER VISIBILITY MODE (via dialog)
// ============================================================================

if (mode == "toggle") {
    def layerStates = discoverLayerStates()

    if (layerStates.size() == 0) {
        Dialogs.showErrorMessage("Error",
            "No layer information found in measurements.\n" +
            "Detections may have been imported with an older version of this script.")
        return
    }

    // Build toggle options showing current state
    def options = layerStates.sort { it.value.position }.collect { name, info ->
        def state = info.hidden ? "SHOW" : "HIDE"
        "${state} '${name}'"
    }

    def choice = Dialogs.showChoiceDialog(
        "Toggle layer",
        "Select a layer to show or hide:\n(After this, use Shift+1/2/3 for instant toggle)",
        options as String[],
        options.last()
    )

    if (choice == null) {
        println "Cancelled."
        registerKeyboardToggle()
        return
    }

    def toggleMatch = (choice =~ /^(SHOW|HIDE) '(.+)'$/)
    if (!toggleMatch.find()) {
        println "ERROR: Could not parse selection"
        return
    }

    def targetLayer = toggleMatch.group(2)
    def targetPos = layerStates[targetLayer].position
    toggleLayerByPosition(targetPos)
    registerKeyboardToggle()
    return
}

// ============================================================================
// IMPORT MODE (fresh, merge, or clear_and_import)
// ============================================================================

// Clear existing if requested
if (mode == "clear_and_import") {
    println "\nClearing ${existingDetections.size()} existing detections..."
    removeObjects(existingDetections, false)
    def props = imageData.getProperties()
    props.keySet().removeAll(props.keySet().findAll { it.startsWith("_layer_classes_") })
    fireHierarchyUpdate()
}

// Prompt for GeoJSON file
def geojsonFile = Dialogs.promptForFile("Select GeoJSON file", null, "GeoJSON", ".geojson")
if (geojsonFile == null) {
    println "Cancelled."
    registerKeyboardToggle()
    return
}

println "GeoJSON: ${geojsonFile.name}"

// Derive layer name from filename, ensure uniqueness
def baseLayerName = geojsonFile.name.replace(".geojson", "")
def knownLayers = (mode == "merge") ? discoverLayers() : []
def layerName = uniqueLayerName(baseLayerName, knownLayers)
def layerPosition = knownLayers.size()

println "Layer name: '${layerName}' (position ${layerPosition})"
if (layerName != baseLayerName) {
    println "  (renamed from '${baseLayerName}' to avoid duplicate)"
}

// Look for classifications file
def classificationsFile = new File(geojsonFile.absolutePath.replace(".geojson", ".classifications.json"))

if (classificationsFile.exists()) {
    println "Found classifications: ${classificationsFile.name}"
    def classText = classificationsFile.text
    def classArray = JsonParser.parseString(classText).getAsJsonArray()
    classArray.each { item ->
        def obj = item.getAsJsonObject()
        def name = obj.get("name").getAsString()
        if (obj.has("rgb")) {
            def rgbArray = obj.get("rgb").getAsJsonArray()
            phenotypeColors[name] = [
                rgbArray.get(0).getAsInt(),
                rgbArray.get(1).getAsInt(),
                rgbArray.get(2).getAsInt()
            ]
        }
    }
    println "Loaded ${phenotypeColors.size()} phenotype colors"
} else {
    println "No classifications file found, will use auto-generated colors"
}

// Load GeoJSON
println "\nLoading GeoJSON..."
def geojsonText = geojsonFile.text
def geojson = JsonParser.parseString(geojsonText).getAsJsonObject()
def features = geojson.get("features").getAsJsonArray()
println "Found ${features.size()} cells"

// Pre-scan class names and build mapping for this layer
def classNameSet = [] as LinkedHashSet
features.each { f ->
    def props = f.getAsJsonObject().get("properties")?.getAsJsonObject()
    def classification = props?.get("classification")?.getAsJsonObject()
    def name = classification?.get("name")?.getAsString() ?: "Unclassified"
    classNameSet.add(name)
}
def classNameList = classNameSet.toList()
def classIdMap = [:]
classNameList.eachWithIndex { name, idx -> classIdMap[name] = idx }

// Store mapping in image properties for toggle
def mappingKey = "_layer_classes_${layerName}"
imageData.getProperties().put(mappingKey, classNameList.join("||"))
println "Stored class mapping: ${classNameList.join(', ')}"

// Build coordinate map for merge mode
def existingMap = [:]
if (mode == "merge") {
    println "\nBuilding coordinate index..."
    def currentDetections = getDetectionObjects()
    currentDetections.each { det ->
        def cx = det.getROI().getCentroidX()
        def cy = det.getROI().getCentroidY()
        def key = "${Math.round(cx * 100)}:${Math.round(cy * 100)}"
        existingMap[key] = det
    }
    println "Indexed ${existingMap.size()} existing detections"
}

// Count classifications in file
println "\nClassifications in file:"
def counts = [:].withDefault { 0 }
features.each { f ->
    def props = f.getAsJsonObject().get("properties")?.getAsJsonObject()
    def classification = props?.get("classification")?.getAsJsonObject()
    def name = classification?.get("name")?.getAsString() ?: "Unclassified"
    counts[name]++
}
counts.sort { -it.value }.each { name, count ->
    def pct = (count / features.size() * 100).round(1)
    def rgb = phenotypeColors[name] ?: [128, 128, 128]
    println "  ${name}: ${count} (${pct}%) RGB(${rgb.join(',')})"
}

// Import cells
println "\nImporting cells..."
def plane = ImagePlane.getDefaultPlane()
def radius = CELL_RADIUS_UM / pixelSize
def prefix = "[${layerName}] "
def detections = []
def toRemove = []
def mergedCount = 0
def newCount = 0
def errorCount = 0

features.eachWithIndex { featureElement, idx ->
    try {
        def feature = featureElement.getAsJsonObject()
        def geometry = feature.get("geometry").getAsJsonObject()
        def geomType = geometry.get("type").getAsString()
        def coords = geometry.get("coordinates").getAsJsonArray()

        // Build ROI based on geometry type (Polygon or Point)
        def roi
        def x_px
        def y_px

        if (geomType == "Polygon") {
            // Polygon: coordinates is [ring] where ring is [[x,y], [x,y], ...]
            def ring = coords.get(0).getAsJsonArray()
            def xCoords = new double[ring.size()]
            def yCoords = new double[ring.size()]
            for (int p = 0; p < ring.size(); p++) {
                def pt = ring.get(p).getAsJsonArray()
                xCoords[p] = pt.get(0).getAsDouble()
                yCoords[p] = pt.get(1).getAsDouble()
            }
            roi = ROIs.createPolygonROI(xCoords, yCoords, plane)
            x_px = roi.getCentroidX()
            y_px = roi.getCentroidY()
        } else {
            // Point: coordinates is [x, y]
            x_px = coords.get(0).getAsDouble()
            y_px = coords.get(1).getAsDouble()
            roi = ROIs.createEllipseROI(
                x_px - radius, y_px - radius,
                radius * 2, radius * 2, plane
            )
        }

        def props = feature.get("properties")?.getAsJsonObject()
        def classification = props?.get("classification")?.getAsJsonObject()
        def className = classification?.get("name")?.getAsString() ?: "Unclassified"
        def classId = classIdMap.containsKey(className) ? classIdMap[className] : -1

        def coordKey = "${Math.round(x_px * 100)}:${Math.round(y_px * 100)}"
        def existingDet = (mode == "merge") ? existingMap[coordKey] : null

        if (existingDet != null) {
            // MERGE: combine classification labels
            def existingClassName = existingDet.getPathClass()?.getName() ?: "Unknown"
            def combinedName = "${existingClassName} | ${className}"
            def combinedClass = getPathClass(combinedName)

            def merged = PathObjects.createDetectionObject(roi, combinedClass)

            // Copy all existing measurements
            def existingML = existingDet.getMeasurementList()
            for (int i = 0; i < existingML.size(); i++) {
                merged.getMeasurementList().put(
                    existingML.getMeasurementName(i),
                    existingML.getMeasurementValue(i)
                )
            }

            // Add new measurements with layer prefix
            def measurements = props?.get("measurements")?.getAsJsonObject()
            if (measurements != null) {
                measurements.entrySet().each { entry ->
                    try {
                        merged.getMeasurementList().put(
                            "${prefix}${entry.key}",
                            entry.value.getAsDouble()
                        )
                    } catch (Exception e) {}
                }
            }
            merged.getMeasurementList().put("${prefix}_position", (double) layerPosition)
            merged.getMeasurementList().put("${prefix}_class_id", (double) classId)
            merged.getMeasurementList().put("${prefix}_hidden", 0.0)
            merged.getMeasurementList().close()

            detections.add(merged)
            toRemove.add(existingDet)
            existingMap.remove(coordKey)
            mergedCount++
        } else {
            // NEW: create fresh detection
            def pathClass = getPathClass(className)

            def detection = PathObjects.createDetectionObject(roi, pathClass)

            def measurements = props?.get("measurements")?.getAsJsonObject()
            if (measurements != null) {
                measurements.entrySet().each { entry ->
                    try {
                        detection.getMeasurementList().put(
                            "${prefix}${entry.key}",
                            entry.value.getAsDouble()
                        )
                    } catch (Exception e) {}
                }
            }
            detection.getMeasurementList().put("${prefix}_position", (double) layerPosition)
            detection.getMeasurementList().put("${prefix}_class_id", (double) classId)
            detection.getMeasurementList().put("${prefix}_hidden", 0.0)
            detection.getMeasurementList().close()

            detections.add(detection)
            newCount++
        }

        if ((idx + 1) % 25000 == 0) {
            println "  Processed ${idx + 1}/${features.size()}"
        }
    } catch (Exception e) {
        errorCount++
    }
}

// Remove old detections that were merged, then add new/merged ones
if (mode == "merge" && toRemove.size() > 0) {
    removeObjects(toRemove, false)
}
addObjects(detections)
fireHierarchyUpdate()

// Summary
println "\n" + "=" * 60
if (mode == "merge" && mergedCount > 0) {
    println "DONE! Added layer '${layerName}' (position ${layerPosition})"
    println "  Merged: ${mergedCount} cells"
    if (newCount > 0) println "  New (unmatched): ${newCount} cells"
    def unmatched = existingMap.size()
    if (unmatched > 0) {
        println "  ${unmatched} existing detections had no match in new file"
    }
} else {
    println "DONE! Imported ${detections.size()} cells as layer '${layerName}'"
}
if (errorCount > 0) {
    println "  WARNING: ${errorCount} cells skipped due to parsing errors"
}

// Register keyboard shortcuts
registerKeyboardToggle()

println "\n" + "=" * 60
println "Layer toggle shortcuts are now active!"
println "Use Shift+1, Shift+2, etc. to toggle layers on/off"
println "=" * 60
