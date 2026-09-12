import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// The conversation / task stream, composed like the reference: an avatar
// column (you / a small JARVIS orb / a state marker), a name + time line,
// then the body. User requests sit in a raised card; JARVIS answers are
// integrated body text; system, error, unknown and approval entries carry
// their own color. Long content stays readable and selectable.
ListView {
    id: feed
    model: bridge.feed
    spacing: 18
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
            // You: a soft disc with a person mark.
            // You: the operator's initial on a soft disc.
            Rectangle {
                visible: row.isUser
                anchors.fill: parent; radius: 25
                color: Theme.alpha(Theme.accentSoft, 0.18); border.width: 1; border.color: Theme.alpha(Theme.accentSoft, 0.5)
                Text { anchors.centerIn: parent; text: bridge.operatorName.charAt(0).toUpperCase(); color: Theme.text; font { family: Theme.font; pixelSize: 19; weight: Font.DemiBold } }
            }
            // JARVIS: a small orb - ring, arc, sphere.
            Item {
                visible: row.isJarvis
                anchors.fill: parent
                Rectangle { anchors.fill: parent; radius: 25; color: "transparent"; border.width: 1; border.color: Theme.alpha(Theme.accent, 0.45) }
                Rectangle { anchors.centerIn: parent; width: 40; height: 40; radius: 20; color: "transparent"; border.width: 2; border.color: Theme.accent; opacity: 0.8 }
                Rectangle { anchors.centerIn: parent; width: 44; height: 44; radius: 22; color: "transparent"; border.width: 4; border.color: Theme.accent; opacity: 0.16 }
                Rectangle { anchors.centerIn: parent; width: 26; height: 26; radius: 13
                    gradient: Gradient { GradientStop { position: 0; color: Qt.lighter(Theme.accent, 1.2) } GradientStop { position: 0.55; color: Theme.bg1 } GradientStop { position: 1; color: Theme.bg0 } }
                    border.width: 1; border.color: Theme.accent }
                Rectangle { anchors.centerIn: parent; width: 60; height: 60; radius: 30; color: Theme.accent; opacity: 0.08; z: -1 }
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
            anchors { left: avatar.right; leftMargin: 18; right: parent.right; rightMargin: 4 }
            height: body.height + (row.isUser || row.loud ? 30 : 14)
            radius: 12
            color: row.isUser ? Theme.surfaceRaised : (row.loud ? Theme.alpha(row.tone, 0.07) : "transparent")
            border.width: row.isUser || row.loud ? 1 : 0
            border.color: row.isUser ? Theme.border : Theme.alpha(row.tone, 0.45)
            // Top sheen on the raised user card.
            Rectangle { visible: row.isUser; anchors { left: parent.left; right: parent.right; top: parent.top; margins: 1 } height: parent.height * 0.5; radius: 11
                gradient: Gradient { GradientStop { position: 0; color: Theme.sheen } GradientStop { position: 1; color: "transparent" } } }
            Column {
                id: body
                x: row.isUser || row.loud ? 18 : 0; y: row.isUser || row.loud ? 14 : 4
                width: parent.width - (row.isUser || row.loud ? 36 : 4)
                spacing: 6
                Row {
                    spacing: 12
                    Text { text: feed.kindLabel(row.kind); color: row.isUser ? Theme.accentSoft : row.tone
                        font { family: Theme.font; pixelSize: row.isUser || row.isJarvis ? 13 : 10; weight: Font.DemiBold
                               letterSpacing: row.isUser ? 0.3 : (row.isJarvis ? 1.6 : 2.2) } }
                    Text { anchors.baseline: parent.children[0].baseline; text: modelData.time; color: Theme.textDim; font { family: Theme.font; pixelSize: Theme.fsMeta } }
                }
                TextEdit {
                    width: parent.width
                    text: modelData.text
                    readOnly: true
                    selectByMouse: true
                    wrapMode: Text.Wrap
                    color: row.kind === "system" ? Theme.textMuted : Theme.text
                    selectionColor: Theme.accentSoft
                    font { family: row.kind === "data" ? Theme.mono : Theme.font; pixelSize: row.kind === "data" ? 12 : Theme.fsBody + 1 }
                    textFormat: TextEdit.PlainText
                }
            }
        }
    }
}
