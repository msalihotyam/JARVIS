import QtQuick
import "../theme"

// A tinted rounded square holding one glyph: the row / card icon of the
// reference (browser, desktop, memory, ...). Glyph-only; no icon pack.
Rectangle {
    id: box
    property string glyph: "◈"
    property color tint: Theme.accent
    property int glyphSize: Math.round(width * 0.42)
    implicitWidth: 36; implicitHeight: 36
    radius: Math.round(width * 0.27)
    color: Theme.alpha(tint, 0.13)
    border.width: 1
    border.color: Theme.alpha(tint, 0.35)
    Rectangle {
        anchors { left: parent.left; right: parent.right; top: parent.top; margins: 1 }
        height: parent.height * 0.5; radius: parent.radius - 1
        gradient: Gradient {
            GradientStop { position: 0.0; color: Theme.alpha(tint, 0.10) }
            GradientStop { position: 1.0; color: "transparent" }
        }
    }
    Text { anchors.centerIn: parent; text: box.glyph; color: box.tint; font { family: Theme.font; pixelSize: box.glyphSize } }
}
