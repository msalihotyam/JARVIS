import QtQuick

// JARVIS's answer: no panel, open article text, then a row of text buttons. The buttons only call face slots.
Item {
    id: turn
    objectName: "jarvisTurn"
    readonly property var tk: face.tokens
    property int hitMin
    property string entryId
    property string time
    property int readMinutes                    // 0: hide the reading time
    property var blocks: []
    property bool collapsed: false              // view state only: the answer shows its first block
    implicitHeight: content.height

    Column {
        id: content
        width: turn.width
        spacing: turn.tk.space_8

        Row {
            objectName: "jarvisMeta"
            height: turn.tk.type_meta_line
            spacing: turn.tk.space_10
            Text {
                objectName: "jarvisWho"
                anchors.verticalCenter: parent.verticalCenter
                text: "JARVIS"
                textFormat: Text.PlainText
                color: turn.tk.color_accent
                font.weight: Font.DemiBold
                font.pixelSize: turn.tk.type_meta_size
            }
            Text {
                objectName: "jarvisTime"
                anchors.verticalCenter: parent.verticalCenter
                width: Math.min(implicitWidth, Math.max(0, content.width - x - (readTime.visible ? readTime.width + parent.spacing : 0)))
                elide: Text.ElideRight
                text: turn.time
                textFormat: Text.PlainText
                color: turn.tk.color_text3
                font.pixelSize: turn.tk.type_meta_size
            }
            Text {
                id: readTime
                objectName: "readTime"
                visible: turn.readMinutes > 0
                anchors.verticalCenter: parent.verticalCenter
                text: qsTr("yaklaşık %1 dk okuma").arg(turn.readMinutes)
                textFormat: Text.PlainText
                color: turn.tk.color_text3
                font.pixelSize: turn.tk.type_meta_size
            }
        }

        Blocks {
            objectName: "answerBlocks"
            width: content.width
            blocks: turn.blocks
            collapsed: turn.collapsed
            hitMin: turn.hitMin
        }

        Row {
            objectName: "answerActions"
            TextButton {
                objectName: "copyButton"
                hitMin: turn.hitMin
                label: qsTr("Kopyala")
                onActivated: face.copyText(turn.entryId)
            }
            TextButton {
                objectName: "collapseButton"
                hitMin: turn.hitMin
                label: turn.collapsed ? qsTr("Genişlet") : qsTr("Daralt")
                onActivated: turn.collapsed = !turn.collapsed
            }
            TextButton {
                objectName: "readAloudButton"
                visible: face.talkAvailable
                hitMin: turn.hitMin
                label: qsTr("Sesli oku")
                onActivated: face.talkToggle(turn.entryId)
            }
        }
    }

    component TextButton: Item {
        id: button
        property int hitMin
        property string label
        property string accessibleName: label
        signal activated()
        implicitWidth: Math.max(hitMin, caption.implicitWidth + face.tokens.space_10 * 2)
        implicitHeight: hitMin
        Accessible.role: Accessible.Button
        Accessible.name: accessibleName
        Text {
            id: caption
            anchors.centerIn: parent
            text: button.label
            textFormat: Text.PlainText
            color: area.containsMouse ? face.tokens.color_text1 : face.tokens.color_text2
            font.pixelSize: face.tokens.type_control_size
        }
        MouseArea {
            id: area
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: button.activated()
        }
    }
}
