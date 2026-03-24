/**
 * ============================================================================
 * QUPATH SCRIPT: Import GeoJSON Cell Detections
 * ============================================================================
 *
 * Imports cells from GeoJSON with measurements (marker intensities, morphology).
 * Supports both QuPath-native array format and legacy flat-dict format.
 * Designed for use with FlowPath — stores measurements with raw names
 * (no layer prefix) for direct channel matching.
 *
 * FEATURES:
 *   - Import cell contours (Polygon) or centroids (Point) from GeoJSON
 *   - Store all measurements with raw names (CD45, CD3, area, etc.)
 *   - Supports QuPath-native measurement format (array of name/value)
 *   - Supports legacy flat-dict measurement format for backward compatibility
 *   - Toggle detection visibility with Shift+1
 *
 * USAGE:
 *   1. Open your pyramidal OME-TIFF in QuPath
 *   2. Automate → Show script editor → Paste this script
 *   3. Run (Ctrl+R) → Select your .geojson file
 *   4. Use Shift+1 to toggle detection visibility
 *
 * ============================================================================
 */

import qupath.lib.objects.PathObjects
import qupath.lib.objects.classes.PathClass
import qupath.lib.roi.ROIs
import qupath.lib.regions.ImagePlane
import com.google.gson.JsonParser

// ============================================================================
// CONFIGURATION
// ============================================================================

def CELL_RADIUS_UM = 3.0  // Fallback radius for Point geometries (µm)

// ============================================================================
// HELPERS
// ============================================================================

def getPathClass = { String name ->
    // Auto-generate color from name hash
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

// ============================================================================
// TOGGLE VISIBILITY (Shift+1)
// ============================================================================

def detectionsVisible = true

def registerToggle = {
    def viewer = getCurrentViewer()
    if (viewer == null) return

    def view = viewer.getView()

    // Remove existing handler
    def existing = view.getProperties().get("_detectionToggleHandler")
    if (existing != null) {
        view.removeEventHandler(javafx.scene.input.KeyEvent.KEY_PRESSED, existing)
        view.getProperties().remove("_detectionToggleHandler")
    }

    def handler = { javafx.scene.input.KeyEvent event ->
        if (event.isShiftDown() && event.getCode() == javafx.scene.input.KeyCode.DIGIT1) {
            event.consume()
            detectionsVisible = !detectionsVisible
            def overlayOptions = viewer.getOverlayOptions()
            overlayOptions.setShowDetections(detectionsVisible)
            println(detectionsVisible ? "Detections: visible" : "Detections: hidden")
        }
    } as javafx.event.EventHandler

    view.addEventHandler(javafx.scene.input.KeyEvent.KEY_PRESSED, handler)
    view.getProperties().put("_detectionToggleHandler", handler)
    println "Shift+1 → toggle detection visibility"
}

// ============================================================================
// SCRIPT
// ============================================================================

println "=" * 60
println "QuPath GeoJSON Import (FlowPath compatible)"
println "=" * 60

// Check image
def imageData = getCurrentImageData()
if (imageData == null) {
    Dialogs.showErrorNotification("Error", "Please open an image first!")
    return
}

def server = imageData.getServer()
def pixelSize = server.getPixelCalibration().getAveragedPixelSizeMicrons()
println "Image: ${server.getMetadata().getName()}"
println "Pixel size: ${pixelSize} µm"

// Check existing detections
def existingDetections = getDetectionObjects()
if (existingDetections.size() > 0) {
    def confirm = Dialogs.showConfirmDialog(
        "Existing detections",
        "Found ${existingDetections.size()} existing detections.\nClear all and import fresh?"
    )
    if (!confirm) {
        println "Cancelled."
        registerToggle()
        return
    }
    println "Clearing ${existingDetections.size()} existing detections..."
    removeObjects(existingDetections, false)
    fireHierarchyUpdate()
}

// Prompt for GeoJSON file
def geojsonFile = null
def latch = new java.util.concurrent.CountDownLatch(1)
javafx.application.Platform.runLater {
    def fc = new javafx.stage.FileChooser()
    fc.setTitle("Select GeoJSON file")
    fc.getExtensionFilters().add(
        new javafx.stage.FileChooser.ExtensionFilter("GeoJSON", "*.geojson")
    )
    geojsonFile = fc.showOpenDialog(null)
    latch.countDown()
}
latch.await()
if (geojsonFile == null) {
    println "Cancelled."
    registerToggle()
    return
}

println "GeoJSON: ${geojsonFile.name}"

// Load GeoJSON
println "\nLoading GeoJSON..."
def geojsonText = geojsonFile.text
def geojson = JsonParser.parseString(geojsonText).getAsJsonObject()
def features = geojson.get("features").getAsJsonArray()
println "Found ${features.size()} cells"

// Count classifications
def counts = [:].withDefault { 0 }
features.each { f ->
    def props = f.getAsJsonObject().get("properties")?.getAsJsonObject()
    def classification = props?.get("classification")?.getAsJsonObject()
    def name = classification?.get("name")?.getAsString() ?: "Unclassified"
    counts[name]++
}
println "\nClassifications:"
counts.sort { -it.value }.each { name, count ->
    def pct = (count / features.size() * 100).round(1)
    println "  ${name}: ${count} (${pct}%)"
}

// Import cells
println "\nImporting cells..."
def plane = ImagePlane.getDefaultPlane()
def radius = CELL_RADIUS_UM / pixelSize
def detections = []
def errorCount = 0

features.eachWithIndex { featureElement, idx ->
    try {
        def feature = featureElement.getAsJsonObject()
        def geometry = feature.get("geometry").getAsJsonObject()
        def geomType = geometry.get("type").getAsString()
        def coords = geometry.get("coordinates").getAsJsonArray()

        // Build ROI
        def roi
        if (geomType == "Polygon") {
            def ring = coords.get(0).getAsJsonArray()
            def xCoords = new double[ring.size()]
            def yCoords = new double[ring.size()]
            for (int p = 0; p < ring.size(); p++) {
                def pt = ring.get(p).getAsJsonArray()
                xCoords[p] = pt.get(0).getAsDouble()
                yCoords[p] = pt.get(1).getAsDouble()
            }
            roi = ROIs.createPolygonROI(xCoords, yCoords, plane)
        } else {
            // Point → small ellipse
            def x_px = coords.get(0).getAsDouble()
            def y_px = coords.get(1).getAsDouble()
            roi = ROIs.createEllipseROI(
                x_px - radius, y_px - radius,
                radius * 2, radius * 2, plane
            )
        }

        // Classification
        def props = feature.get("properties")?.getAsJsonObject()
        def classification = props?.get("classification")?.getAsJsonObject()
        def className = classification?.get("name")?.getAsString()
        def pathClass = className ? getPathClass(className) : null

        def detection = PathObjects.createDetectionObject(roi, pathClass)

        // Store measurements with RAW names (no prefix)
        // Supports both QuPath-native array format and legacy flat-dict format
        def measurementsElement = props?.get("measurements")
        if (measurementsElement != null) {
            if (measurementsElement.isJsonArray()) {
                // QuPath-native format: [{"name": "CD45", "value": 1500.5}, ...]
                measurementsElement.getAsJsonArray().each { item ->
                    try {
                        def obj = item.getAsJsonObject()
                        def name = obj.get("name").getAsString()
                        def value = obj.get("value").getAsDouble()
                        detection.getMeasurementList().put(name, value)
                    } catch (Exception e) {}
                }
            } else if (measurementsElement.isJsonObject()) {
                // Legacy flat-dict format: {"CD45": 1500.5, ...}
                measurementsElement.getAsJsonObject().entrySet().each { entry ->
                    try {
                        detection.getMeasurementList().put(
                            entry.key,
                            entry.value.getAsDouble()
                        )
                    } catch (Exception e) {}
                }
            }
        }
        detection.getMeasurementList().close()

        detections.add(detection)

        if ((idx + 1) % 25000 == 0) {
            println "  Processed ${idx + 1}/${features.size()}"
        }
    } catch (Exception e) {
        errorCount++
    }
}

// Add detections
addObjects(detections)
fireHierarchyUpdate()

// Summary
println "\n" + "=" * 60
println "DONE! Imported ${detections.size()} cells"
if (errorCount > 0) {
    println "  WARNING: ${errorCount} cells skipped due to parsing errors"
}

// Register keyboard toggle
registerToggle()

println "\nShift+1 → toggle detection visibility"
println "Open FlowPath for interactive gating"
println "=" * 60
