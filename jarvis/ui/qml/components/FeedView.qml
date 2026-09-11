import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// The conversation / task stream. Not chat bubbles: user requests are a
// short raised line, JARVIS answers are integrated body text, system and
// state notes are muted, errors / unknown / approval carry their own color
// and rail. Long content stays readable and selectable.
ListView {
    id: feed
    model: bridge.feed
    spacing: 14
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
    onCountChanged: Qt.callLater(function() { feed.positionViewAtEnd() })

    function railColor(kind) {
        switch (kind) {
        case "user": return Theme.accentSoft
        case "jarvis": return Theme.accent
        case "error": return Theme.danger
        case "unknown": return Theme.unknownEdge
        case "approval": return Theme.warning
        case "clarify": return Theme.accentSoft
        case "scheduled": return Theme.success
        case "data": return Theme.textMuted
        default: return Theme.textDim
        }
    }
    function kindLabel(kind) {
        switch (kind) {
        case "user": return "YOU"
        case "jarvis": return "JARVIS"
        case "error": return "ERROR"
        case "unknown": return "OUTCOME UNKNOWN"
        case "approval": return "APPROVAL"
        case "clarify": return "JARVIS · NEEDS ONE DETAIL"
        case "scheduled": return "SCHEDULED"
        case "data": return "UNTRUSTED DATA"
        default: return "SYSTEM"
        }
    }

    delegate: Item {
        id: row
        width: feed.width
        height: card.height
        property bool isUser: modelData.kind === "user"
        property bool loud: modelData.kind === "error" || modelData.kind === "unknown" || modelData.kind === "approval"
        // Appearance: fade + short slide, no bounce.
        opacity: 0
        transform: Translate { id: slide; y: 8 }
        Component.onCompleted: { appear.start() }
        ParallelAnimation {
            id: appear
            NumberAnimation { target: row; property: "opacity"; to: 1; duration: Theme.panelIn; easing.type: Easing.OutCubic }
            NumberAnimation { target: slide; property: "y"; to: 0; duration: Theme.panelIn; easing.type: Easing.OutCubic }
        }

        Rectangle {
            id: card
            width: parent.width
            height: content.height + (isUser ? 22 : 16)
            radius: Theme.radiusSmall
            color: isUser ? Theme.surfaceRaised : (loud ? Qt.rgba(railColor(modelData.kind).r, railColor(modelData.kind).g, railColor(modelData.kind).b, 0.08) : "transparent")
            border.width: isUser || loud ? 1 : 0
            border.color: isUser ? Theme.border : Qt.rgba(railColor(modelData.kind).r, railColor(modelData.kind).g, railColor(modelData.kind).b, 0.5)
            Rectangle { x: 0; y: 8; width: 2; height: parent.height - 16; radius: 1; color: feed.railColor(modelData.kind); visible: !isUser }
            Column {
                id: content
                x: 16; y: isUser ? 11 : 8
                width: parent.width - 32
                spacing: 4
                Row {
                    spacing: 10
                    Text { text: feed.kindLabel(modelData.kind); color: feed.railColor(modelData.kind)
                        font { family: Theme.font; pixelSize: 10; letterSpacing: 2; weight: Font.DemiBold } }
                    Text { text: modelData.time; color: Theme.textDim; font { family: Theme.font; pixelSize: 10 } }
                }
                TextEdit {
                    width: parent.width
                    text: modelData.text
                    readOnly: true
                    selectByMouse: true
                    wrapMode: Text.Wrap
                    color: modelData.kind === "system" ? Theme.textMuted : Theme.text
                    selectionColor: Theme.accentSoft
                    font { family: modelData.kind === "data" ? Theme.mono : Theme.font; pixelSize: modelData.kind === "data" ? 12 : Theme.fsBody }
                    textFormat: TextEdit.PlainText
                }
            }
        }
    }
}
