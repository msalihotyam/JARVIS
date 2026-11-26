import QtQuick

// FACEBAR1: Send, 44 x 44. Accent fill and accent_ink glyph only when face.composer.canSend (and the link
// is not unreachable); otherwise dim (disabled_glyph) and a click calls nothing.
Rectangle {
    id: send
    readonly property var t: face.tokens
    readonly property real hit: t.type_control_line + 2 * t.space_12       // 44 px hit target
    readonly property bool live: face.composer.canSend === true && face.link !== "unreachable"
    implicitWidth: hit
    implicitHeight: hit
    radius: t.radius_control
    color: live ? t.color_accent : t.color_surface2
    Accessible.role: Accessible.Button
    Accessible.name: "Gönder"

    Text {
        objectName: "sendGlyph"
        anchors.centerIn: parent
        text: "↑"
        textFormat: Text.PlainText
        color: send.live ? send.t.color_accent_ink : send.t.color_disabled_glyph
        font.pixelSize: send.t.type_h2_size
        font.bold: true
    }
    MouseArea {
        anchors.fill: parent
        onClicked: if (send.live) face.send()
        onDoubleClicked: {}         // connected, so the second click of a double click is not a second send
    }
}
