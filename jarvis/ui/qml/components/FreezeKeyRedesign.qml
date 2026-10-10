import QtQuick
import QtQuick.Shapes

// Freeze key skin 2, the approved redesign (Gate 6G S2, Design board 11
// "Mechanism"): a brushed metal bezel round a maroon face, no caption under
// it, a static halo (K2: it never breathes or grows). `position` comes from
// the established execution state: 0 running (pause bars - the press
// freezes), 1 frozen (a play triangle - the press asks to resume, and a red
// inner ring), between = on its way (the ring at half strength).
Item {
    id: key
    property real position: 0
    property bool hovered: false
    readonly property bool frozen: position === 1
    readonly property real k: width / 44
    objectName: "freezeKeyRedesign"

    Rectangle { anchors.centerIn: parent; width: parent.width + 8 * key.k; height: width; radius: width / 2; color: "#ef334f"; opacity: key.frozen ? 0.10 : 0.05 }
    Rectangle { anchors.fill: parent; radius: width / 2
        gradient: Gradient { GradientStop { position: 0; color: "#eceff3" } GradientStop { position: 0.45; color: "#9aa0a8" } GradientStop { position: 1; color: "#3f444c" } }
    }
    Rectangle { anchors.fill: parent; anchors.margins: 2.5 * key.k; radius: width / 2; color: "#1b0b11" }
    Rectangle {
        objectName: "freezeKeyFace"
        anchors.fill: parent; anchors.margins: 3.5 * key.k; radius: width / 2
        border.width: key.position > 0 ? 1.2 * key.k : 0
        border.color: Qt.rgba(0.85, 0.27, 0.37, key.frozen ? 1 : 0.5)
        gradient: Gradient { GradientStop { position: 0; color: key.hovered ? "#6e2234" : "#5c1c2c" } GradientStop { position: 0.55; color: "#33101b" } GradientStop { position: 1; color: "#1d0810" } }
    }
    Shape { anchors.centerIn: parent; width: 20; height: 22; scale: key.k; preferredRendererType: Shape.CurveRenderer
        ShapePath { strokeColor: key.frozen ? "transparent" : "#f0b3bb"; strokeWidth: 3; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            startX: 7; startY: 5; PathLine { x: 7; y: 17 } PathMove { x: 13; y: 5 } PathLine { x: 13; y: 17 } }
        ShapePath { strokeColor: key.frozen ? "#f4c4ca" : "transparent"; strokeWidth: 2; fillColor: "transparent"; joinStyle: ShapePath.RoundJoin
            startX: 7; startY: 5; PathLine { x: 16.5; y: 11 } PathLine { x: 7; y: 17 } PathLine { x: 7; y: 5 } }
    }
}
