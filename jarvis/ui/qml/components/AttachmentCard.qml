import QtQuick
import "../theme"

// One attachment, the same way everywhere it appears: in the draft, inside a
// reference group and under a sent message. An image shows its real thumbnail
// (JARVIS's own snapshot, aspect kept); a file shows its type and size. A
// click opens the internal preview; the ✕, when offered, removes it from the
// draft. `nested` (inside a group or a sent message) is a small tonal step
// from its parent rather than another framed card. Presentation only -
// nothing here reads a path or grants anything.
Item {
    id: card
    property var file: ({})
    property bool removable: false
    property bool compact: false
    property bool nested: false
    readonly property bool image: file.kind === "image"
    readonly property string name: String(file.name || "")
    readonly property string ext: { var m = name.match(/\.([A-Za-z0-9]{1,5})$/); return m ? m[1].toUpperCase() : "" }
    readonly property int thumb: compact ? 40 : 52
    objectName: "attachment_" + file.id
    signal opened()
    activeFocusOnTab: true
    Accessible.role: Accessible.Button
    Accessible.name: name
    Keys.onReturnPressed: card.opened()
    Keys.onSpacePressed: card.opened()

    function human(bytes) {
        var n = Number(bytes || 0)
        if (n < 1024) return qsTr("%1 B").arg(n)
        if (n < 1024 * 1024) return qsTr("%1 KB").arg(Math.max(1, Math.round(n / 1024)))
        return qsTr("%1 MB").arg((n / (1024 * 1024)).toFixed(1))
    }

    width: Math.min(270, body.width + 18 + thumb + 10 + (removable ? 22 : 0))
    height: thumb + 12
    // Arrival: a short fade, no travel (none at all with ambient effects off).
    opacity: 0
    Component.onCompleted: appear.start()
    NumberAnimation { id: appear; target: card; property: "opacity"; to: 1; duration: Theme.attachMs; easing.type: Easing.OutCubic }

    Rectangle {
        anchors.fill: parent
        radius: 8
        color: hover.hovered ? Theme.alpha(Theme.text, 0.07) : card.nested ? Theme.glassNested : Theme.alpha(Theme.text, 0.045)
        border.width: 1
        border.color: hover.hovered ? Theme.alpha("#49bdfa", 0.35) : card.nested ? Theme.glassNestedEdge : Theme.alpha(Theme.text, 0.08)
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
        // Keyboard focus: a crisp 2 px outline inside the card's own edge.
        Rectangle { anchors.fill: parent; anchors.margins: 1; radius: parent.radius - 1; color: "transparent"
                    border.width: 2; border.color: Theme.materialGlint; visible: card.activeFocus }
    }
    HoverHandler { id: hover }
    // What it is: the image itself, or a type tile for a file.
    Rectangle {
        id: tile
        x: 6; y: 6; width: card.thumb; height: card.thumb; radius: 6
        color: card.image ? "#06111e" : Theme.alpha("#36aaff", 0.10)
        border.width: 1; border.color: Theme.alpha("#49bdfa", card.image ? 0.25 : 0.32)
        clip: true
        Image {
            objectName: "attachmentThumb_" + card.file.id
            visible: card.image
            anchors.fill: parent; anchors.margins: 1
            source: card.image ? (card.file.preview || "") : ""
            fillMode: Image.PreserveAspectFit
            asynchronous: true
            sourceSize.width: 256
            smooth: true
        }
        // A file: a small drawn page (no glyph the fonts might lack) and its type.
        Column {
            visible: !card.image
            anchors.centerIn: parent; spacing: 3
            Item { anchors.horizontalCenter: parent.horizontalCenter; width: card.compact ? 12 : 15; height: card.compact ? 15 : 19
                   Rectangle { anchors.fill: parent; radius: 2; color: "transparent"; border.width: 1; border.color: Theme.materialBlue }
                   Rectangle { x: 3; y: parent.height * 0.42; width: parent.width - 6; height: 1; color: Theme.materialBlue; opacity: 0.8 }
                   Rectangle { x: 3; y: parent.height * 0.42 + 4; width: parent.width - 6; height: 1; color: Theme.materialBlue; opacity: 0.8 } }
            Text { anchors.horizontalCenter: parent.horizontalCenter; text: card.ext; color: Theme.materialBlue
                   font: Theme.label }
        }
    }
    Column {
        id: body
        x: tile.x + tile.width + 10
        anchors.verticalCenter: parent.verticalCenter
        // Wide enough for the longer of the two lines, capped so cards stay compact.
        width: Math.min(170, Math.max(nameText.implicitWidth, metaText.implicitWidth))
        spacing: 2
        // The name leads, the type and size read beneath it.
        Text { id: nameText; objectName: "attachmentName_" + card.file.id; renderType: Theme.textRender
               width: parent.width; text: card.name; elide: Text.ElideMiddle; color: Theme.text
               font: card.compact ? Theme.caption : Theme.body }
        Text { id: metaText; objectName: "attachmentMeta_" + card.file.id; renderType: Theme.textRender
               width: parent.width; elide: Text.ElideRight; color: Theme.textMuted
               text: (card.image ? qsTr("Image") : (card.ext || qsTr("File"))) + " · " + card.human(card.file.bytes)
               font: Theme.caption }
    }
    MouseArea {
        objectName: "attachmentOpen_" + card.file.id
        anchors.fill: parent
        anchors.rightMargin: card.removable ? 24 : 0
        cursorShape: Qt.PointingHandCursor
        onClicked: card.opened()
    }
    Text {
        objectName: "attachmentRemove_" + card.file.id
        visible: card.removable
        anchors.right: parent.right; anchors.rightMargin: 7; anchors.top: parent.top; anchors.topMargin: 5
        text: "✕"; color: removeArea.containsMouse ? Theme.danger : Theme.textMuted
        font: Theme.caption
        Accessible.role: Accessible.Button
        Accessible.name: qsTr("Remove this attachment")
        MouseArea { id: removeArea; anchors.fill: parent; anchors.margins: -6; hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor; onClicked: bridge.removeAttachment(card.file.id) }
    }
}
