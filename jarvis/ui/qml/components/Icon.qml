import QtQuick
import QtQuick.Shapes
import "icons.js" as Icons

// Gate 6G K1: THE drawn icon, chosen by name (icons.js holds the set). Tint
// and size are properties; the paths are scaled from their 18-unit grid with
// a proportional stroke, so every size keeps one visual language. No icon is
// a font glyph. An unknown name draws the crossed-box fallback and warns; an
// empty name draws nothing (a control that has no icon).
Shape {
    id: icon
    property string name: ""
    property color tint: "#f2f8ff"
    property real size: 18
    readonly property bool known: name === "" || Icons.paths.hasOwnProperty(name)
    readonly property var entry: name === "" ? "" : known ? Icons.paths[name] : Icons.paths["missing"]
    // Never a zero scale: a control may size its icon from a width that is still 0.
    readonly property real k: Math.max(size, 1) / 18
    width: size; height: size
    preferredRendererType: Shape.CurveRenderer
    // Checked here, not through `known`: that binding may not have re-evaluated yet.
    onNameChanged: if (name !== "" && !Icons.paths.hasOwnProperty(name)) console.warn("Icon: unknown icon name \"" + name + "\"")
    ShapePath {
        scale: Qt.size(icon.k, icon.k)
        strokeColor: icon.tint; strokeWidth: 1.6 * icon.k; fillColor: "transparent"
        joinStyle: ShapePath.RoundJoin; capStyle: ShapePath.RoundCap
        PathSvg { path: typeof icon.entry === "string" ? icon.entry : icon.entry.s }
    }
    ShapePath {
        scale: Qt.size(icon.k, icon.k)
        strokeWidth: -1; fillColor: icon.tint
        PathSvg { path: typeof icon.entry === "string" ? "" : icon.entry.f || "" }
    }
}
