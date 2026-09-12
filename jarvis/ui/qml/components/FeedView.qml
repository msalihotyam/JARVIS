import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// The conversation / task stream, composed like the reference: an avatar
// column (the operator's initial on a blue disc / a JARVIS ring / a state
// marker), a name + time line, then the body. User requests sit in a
// raised card no wider than the reference's; JARVIS answers are integrated
// body text; system, error, unknown and approval entries carry their own
// color. Long content stays readable and selectable.
ListView {
    id: feed
    model: bridge.feed
    spacing: 20
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
        case "user": return "You"
        case "jarvis": return "JARVIS"
        case "error": return "ERROR"
        case "unknown": return "OUTCOME UNKNOWN"
        case "approval": return "APPROVAL"
        case "clarify": return "JARVIS · needs one detail"
        case "scheduled": return "SCHEDULED"
        case "data": return "UNTRUSTED DATA"
        default: return "SYSTEM"
        }
    }

    delegate: Item {
        id: row
        width: feed.width
        height: Math.max(avatar.height, card.height)
        readonly property string kind: modelData.kind
        readonly property bool isUser: kind === "user"
        readonly property bool isJarvis: kind === "jarvis" || kind === "clarify"
        readonly property bool loud: kind === "error" || kind === "unknown" || kind === "approval"
        readonly property bool boxed: isUser || loud
        readonly property color tone: feed.railColor(kind)
        // Appearance: fade + short slide, no bounce.
        opacity: 0
        transform: Translate { id: slide; y: 8 }
        Component.onCompleted: { appear.start() }
        ParallelAnimation {
            id: appear
            NumberAnimation { target: row; property: "opacity"; to: 1; duration: Theme.panelIn; easing.type: Easing.OutCubic }
            NumberAnimation { target: slide; property: "y"; to: 0; duration: Theme.panelIn; easing.type: Easing.OutCubic }
        }

        // -- avatar column ---------------------------------------------------
        Item {
            id: avatar
            width: 50; height: 50
            // You: the operator's initial on a blue disc.
            Rectangle {
                visible: row.isUser
                anchors.fill: parent; radius: 25
                color: Theme.alpha(Theme.accentSoft, 0.5); border.width: 1; border.color: Theme.alpha(Theme.accentSoft, 0.7)
                Rectangle { anchors { left: parent.left; right: parent.right; top: parent.top; margins: 1 } height: 24; radius: 24
                    gradient: Gradient { GradientStop { position: 0; color: Qt.rgba(1, 1, 1, 0.12) } GradientStop { position: 1; color: "transparent" } } }
                Text { anchors.centerIn: parent; text: bridge.operatorName.charAt(0).toUpperCase(); color: "#f2f8ff"; font { family: Theme.font; pixelSize: 20; weight: Font.DemiBold } }
            }
            // JARVIS: the luminous ring of the brand orb, small - a thin bright
            // ring inside a soft glow, with the faint calibrated outer ring.
            Item {
                visible: row.isJarvis
                anchors.fill: parent
                Rectangle { anchors.centerIn: parent; width: 60; height: 60; radius: 30; color: Theme.accent; opacity: 0.07 }
                Rectangle { anchors.fill: parent; radius: 25; color: "transparent"; border.width: 1; border.color: Theme.alpha(Theme.accent, 0.35) }
                Repeater { model: 6; Rectangle { width: 2.5; height: 2.5; radius: 1.25; color: Theme.accent; opacity: 0.75; x: 23.75; y: -1; transform: Rotation { origin.x: 1.25; origin.y: 26; angle: index * 60 + 15 } } }
                Rectangle { anchors.centerIn: parent; width: 40; height: 40; radius: 20; color: "transparent"; border.width: 7; border.color: Theme.accent; opacity: 0.22 }
                Rectangle { anchors.centerIn: parent; width: 36; height: 36; radius: 18; color: Theme.alpha(Theme.bg0, 0.6); border.width: 3; border.color: Qt.lighter(Theme.accent, 1.08) }
            }
            // Everything else: a state marker on the rail.
            Rectangle {
                visible: !row.isUser && !row.isJarvis
                anchors.centerIn: parent; width: 30; height: 30; radius: 15
                color: Theme.alpha(row.tone, 0.12); border.width: 1; border.color: Theme.alpha(row.tone, 0.5)
                Rectangle { anchors.centerIn: parent; width: 8; height: 8; radius: 4; color: row.tone }
            }
        }

        // -- body --------------------------------------------------------------
        Rectangle {
            id: card
            x: avatar.width + 18
            // The reference wraps both the request card and JARVIS's text at
            // about 640 px rather than spanning the whole flow.
            width: row.boxed ? Math.min(row.width - x - 4, 640) : row.width - x - 4
            readonly property real textWidth: row.boxed ? width - 36 : Math.min(width - 4, 640)
            height: body.height + (row.boxed ? 30 : 14)
            radius: 12
            // The request card of the reference: quiet darker glass, a barely-there border.
            color: row.isUser ? Theme.alpha(Theme.bg0, 0.34) : (row.loud ? Theme.alpha(row.tone, 0.07) : "transparent")
            border.width: row.boxed ? 1 : 0
            border.color: row.isUser ? Theme.alpha(Theme.accent, 0.14) : Theme.alpha(row.tone, 0.45)
            Rectangle { visible: row.isUser; anchors { left: parent.left; right: parent.right; top: parent.top; margins: 1 } height: parent.height * 0.5; radius: 11
                gradient: Gradient { GradientStop { position: 0; color: Theme.alpha(Theme.accent, 0.035) } GradientStop { position: 1; color: "transparent" } } }
            Column {
                id: body
                x: row.boxed ? 18 : 0; y: row.boxed ? 14 : 4
                width: card.textWidth
                spacing: 7
                Row {
                    spacing: 12
                    Text { text: feed.kindLabel(row.kind); color: row.isUser ? "#eaf4ff" : row.tone
                        font { family: Theme.font; pixelSize: row.isUser || row.isJarvis ? 15 : 10; weight: Font.DemiBold
                               letterSpacing: row.isUser ? 0.2 : (row.isJarvis ? 1.6 : 2.2) } }
                    Text { anchors.baseline: parent.children[0].baseline; text: modelData.time; color: Theme.textMuted; font { family: Theme.font; pixelSize: 13 } }
                }
                TextEdit {
                    width: parent.width
                    text: modelData.text
                    readOnly: true
                    selectByMouse: true
                    wrapMode: Text.Wrap
                    color: row.kind === "system" ? Theme.textMuted : Theme.text
                    selectionColor: Theme.accentSoft
                    font { family: row.kind === "data" ? Theme.mono : Theme.font; pixelSize: row.kind === "data" ? 12 : 16 }
                    textFormat: TextEdit.PlainText
                }
            }
        }
    }
}
