import QtQuick

// FACEBAR1: reply mode, Yazılı | Sesli, 44 high. A click calls face.modeSet; face.composer.mode picks the
// selected segment, which gets a line2 fill (the same colour as the frame, so it meets the border cleanly).
Rectangle {
    id: sw
    readonly property var t: face.tokens
    readonly property real hit: t.type_control_line + 2 * t.space_12       // 44 px hit target
    implicitWidth: segs.implicitWidth
    implicitHeight: hit
    radius: t.radius_control
    color: t.color_surface1
    border.width: 1
    border.color: t.color_line2

    Row {
        id: segs
        Repeater {
            model: [{ key: "yazili", label: "Yazılı", name: "modeYazili" },
                    { key: "sesli", label: "Sesli", name: "modeSesli" }]
            Item {
                id: seg
                required property var modelData
                readonly property bool selected: face.composer.mode === modelData.key
                objectName: modelData.name
                width: Math.max(sw.hit, label.implicitWidth + 2 * sw.t.space_14)
                height: sw.hit
                Accessible.role: Accessible.RadioButton
                Accessible.name: modelData.label
                Accessible.checked: selected

                Rectangle {
                    visible: seg.selected
                    anchors.fill: parent
                    radius: sw.t.radius_control
                    color: sw.t.color_line2
                }
                Text {
                    id: label
                    anchors.centerIn: parent
                    text: seg.modelData.label
                    textFormat: Text.PlainText
                    color: seg.selected ? sw.t.color_text1 : sw.t.color_text2
                    font.pixelSize: sw.t.type_control_size
                }
                MouseArea {
                    anchors.fill: parent
                    onClicked: face.modeSet(seg.modelData.key)
                }
            }
        }
    }
}
