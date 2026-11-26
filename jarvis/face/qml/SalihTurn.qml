import QtQuick

// Salih's turn: a thin panel. Its text arrives as one plain "p" block and is never parsed as markdown.
Rectangle {
    id: turn
    objectName: "salihTurn"
    readonly property var tk: face.tokens
    property int hitMin
    property string sourceLabel
    property string time
    property int voiceSeconds: -1               // -1: not a voice turn
    property var blocks: []

    function clock(seconds) {                   // m:ss, or h:mm:ss past an hour
        var d = new Date(0)
        d.setUTCSeconds(seconds)
        var ss = ("0" + d.getUTCSeconds()).slice(-2)
        if (d.getUTCHours() > 0)
            return d.getUTCHours() + ":" + ("0" + d.getUTCMinutes()).slice(-2) + ":" + ss
        return d.getUTCMinutes() + ":" + ss
    }

    color: tk.color_surface1
    border.color: tk.color_line1
    border.width: 1
    radius: tk.radius_panel
    implicitHeight: content.y + content.height + tk.space_12

    Column {
        id: content
        objectName: "salihContent"
        x: turn.tk.space_20
        y: turn.tk.space_14
        width: turn.width - turn.tk.space_20 * 2
        spacing: turn.tk.space_4

        Row {
            objectName: "salihMeta"
            height: turn.tk.type_meta_line
            spacing: turn.tk.space_8
            Text {
                objectName: "salihWho"
                anchors.verticalCenter: parent.verticalCenter
                text: qsTr("Sen")
                textFormat: Text.PlainText
                color: turn.tk.color_text1
                font.weight: Font.DemiBold
                font.pixelSize: turn.tk.type_meta_size
            }
            Rectangle {
                objectName: "sourcePill"
                visible: turn.sourceLabel !== ""
                anchors.verticalCenter: parent.verticalCenter
                width: Math.min(sourceText.implicitWidth + turn.tk.space_8 * 2, content.width / 2)
                height: turn.tk.type_meta_line
                radius: turn.tk.radius_pill
                color: turn.tk.color_surface2
                Text {
                    id: sourceText
                    anchors.centerIn: parent
                    width: Math.max(0, parent.width - turn.tk.space_8 * 2)
                    elide: Text.ElideRight
                    text: turn.sourceLabel
                    textFormat: Text.PlainText
                    color: turn.tk.color_text2
                    font.pixelSize: turn.tk.type_meta_size
                }
            }
            Text {
                objectName: "salihTime"
                anchors.verticalCenter: parent.verticalCenter
                width: Math.min(implicitWidth, Math.max(0, content.width - x - (voiceMeta.visible ? voiceMeta.width + parent.spacing : 0)))
                elide: Text.ElideRight
                text: turn.time
                textFormat: Text.PlainText
                color: turn.tk.color_text3
                font.pixelSize: turn.tk.type_meta_size
            }
            Row {
                id: voiceMeta
                objectName: "voiceMeta"
                visible: turn.voiceSeconds >= 0
                anchors.verticalCenter: parent.verticalCenter
                spacing: turn.tk.space_4
                Item {                          // the small mic glyph: a capsule on a base line
                    objectName: "micGlyph"
                    anchors.verticalCenter: parent.verticalCenter
                    width: turn.tk.space_8
                    height: turn.tk.space_12
                    Rectangle {
                        x: 1
                        width: turn.tk.space_6
                        height: turn.tk.space_8
                        radius: width / 2
                        color: "transparent"
                        border.color: turn.tk.color_text3
                        border.width: 1
                    }
                    Rectangle { y: turn.tk.space_10; width: turn.tk.space_8; height: 1; color: turn.tk.color_text3 }
                }
                Text {
                    objectName: "voiceText"
                    anchors.verticalCenter: parent.verticalCenter
                    text: qsTr("sesli · %1").arg(turn.clock(turn.voiceSeconds))
                    textFormat: Text.PlainText
                    color: turn.tk.color_text3
                    font.pixelSize: turn.tk.type_meta_size
                }
            }
        }

        Blocks {
            objectName: "salihBlocks"
            width: content.width
            blocks: turn.blocks
            salih: true
            hitMin: turn.hitMin
        }
    }
}
