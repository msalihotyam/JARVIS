import QtQuick
import QtQuick.Controls.Basic
import "../theme"
import "../components"

// The internal look at one attachment: an image large and in proportion, or a
// text-like file's snapshotted text, read-only and scrollable. Dark
// translucent backdrop; the close icon, Escape and a click outside close it.
//
// Display only. It shows exactly what JARVIS already holds for the attachment
// - the snapshot copy and the text taken at attach time - and never opens the
// original path, a browser or any other program.
Item {
    id: preview
    objectName: "attachmentPreview"
    property bool open: false
    property var file: ({})
    readonly property string kind: String(file.kind || "")
    anchors.fill: parent
    visible: open || scrim.opacity > 0
    enabled: open
    z: 600
    function show(item) { file = item || ({}); open = true; frame.forceActiveFocus() }
    function close() { open = false }

    Rectangle {
        id: scrim
        anchors.fill: parent; color: "#000000"
        opacity: preview.open ? 0.62 : 0
        Behavior on opacity { NumberAnimation { duration: Theme.panelIn } }
        MouseArea { anchors.fill: parent; onClicked: preview.close() }
        WheelHandler { }
    }
    Rectangle {
        id: frame
        objectName: "attachmentPreviewFrame"
        readonly property real maxW: preview.width * 0.8
        readonly property real maxH: preview.height * 0.82
        width: preview.kind === "image" ? Math.min(maxW, Math.max(320, picture.paintedWidth + 32)) : Math.min(maxW, 760)
        // A short text file gets a short frame; a long one scrolls inside the bound.
        height: preview.kind === "image" ? Math.min(maxH, Math.max(200, picture.paintedHeight + head.height + 40))
                                         : Math.min(maxH, Math.max(220, body.height + head.height + 44))
        anchors.centerIn: parent
        radius: 12
        // The same foreground material as every open panel: one fine edge, a quiet drop.
        color: Theme.glassOverlay
        border.width: 1; border.color: Theme.glassOverlayEdge
        Rectangle { z: -1; anchors.fill: parent; anchors.topMargin: 6; anchors.leftMargin: 2; anchors.rightMargin: -2
                    anchors.bottomMargin: -6; radius: parent.radius + 2; color: "#000000"; opacity: 0.32 }
        opacity: preview.open ? 1 : 0
        scale: preview.open ? 1 : 0.98
        Behavior on opacity { NumberAnimation { duration: Theme.panelIn } }
        Behavior on scale { NumberAnimation { duration: Theme.panelIn; easing.type: Easing.OutCubic } }
        Keys.onEscapePressed: preview.close()
        MouseArea { anchors.fill: parent }          // a click inside stays inside
        WheelHandler { }

        Item {
            id: head
            x: 16; y: 12; width: parent.width - 32; height: 36
            Column {
                anchors.verticalCenter: parent.verticalCenter; width: parent.width - 40; spacing: 1
                Text { objectName: "attachmentPreviewName"; renderType: Theme.textRender; width: parent.width; elide: Text.ElideMiddle
                       text: String(preview.file.name || ""); color: Theme.text
                       font: Theme.subtitle }
                Text { objectName: "attachmentPreviewMeta"; renderType: Theme.textRender; width: parent.width; elide: Text.ElideRight; color: Theme.textMuted
                       text: (preview.kind === "image" ? qsTr("Image") : qsTr("Text file")) + " · " + String(preview.file.media_type || "")
                             + " · " + card.human(preview.file.bytes)
                             + (preview.file.truncated ? " · " + qsTr("shown up to the first %1 characters").arg("20000") : "")
                       font: Theme.caption }
            }
            IconButton { objectName: "attachmentPreviewClose"; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                         icon: "close"; tip: qsTr("Close"); onClicked: preview.close() }
        }
        AttachmentCard { id: card; visible: false }    // for its size formatting only

        // The image, in proportion, never larger than the frame.
        Image {
            id: picture
            objectName: "attachmentPreviewImage"
            visible: preview.kind === "image"
            x: 16; y: head.y + head.height + 8
            width: parent.width - 32; height: parent.height - y - 16
            source: preview.kind === "image" ? (preview.file.preview || "") : ""
            fillMode: Image.PreserveAspectFit
            asynchronous: true
            smooth: true
            // The natural size, so the frame can shrink to a small image.
            readonly property real paintedWidth: sourceSize.width > 0 ? Math.min(sourceSize.width, frame.maxW - 32) : 320
            readonly property real paintedHeight: sourceSize.width > 0 ? paintedWidth * sourceSize.height / sourceSize.width : 200
        }
        // A text-like file: read-only, scrollable, bounded to what was snapshotted.
        Flickable {
            id: reader
            visible: preview.kind === "text"
            x: 16; y: head.y + head.height + 8
            width: parent.width - 32; height: parent.height - y - 16
            clip: true
            contentHeight: body.height
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded; width: 6 }
            TextEdit {
                id: body
                objectName: "attachmentPreviewText"
                width: reader.width - 12
                text: preview.kind === "text" ? String(preview.file.text || "") : ""
                readOnly: true
                selectByMouse: true
                wrapMode: TextEdit.Wrap
                textFormat: TextEdit.PlainText
                renderType: Theme.textRender
                color: Theme.text
                selectionColor: Theme.accentSoft
                font: Theme.tracked(Theme.code, 0)
            }
        }
    }
}
