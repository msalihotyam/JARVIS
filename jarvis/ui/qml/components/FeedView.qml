import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// The conversation / task stream: an avatar column (the operator's initial
// on a glossy disc / the JARVIS ring / a state marker), a name + time line,
// then the body. Requests sit in a compact bounded glass block; JARVIS
// answers sit on a lighter, quieter glass surface under their label;
// system, error, unknown and approval entries carry their own colour. Long
// content stays readable and selectable.
//
// `referenceLayout` (the Canonical Command Center) takes the approved
// conversation helper's proportions: a 60 px avatar column, content at the
// helper's inset, the request at ~69 % of the flow and answers to its right
// edge. Without it (the Command preset) the earlier proportions hold.
ListView {
    id: feed
    model: bridge.feed
    spacing: referenceLayout ? 20 : 18
    property bool pipelineEnabled: false
    property bool referenceLayout: false
    // The avatar column scales with the surface, so its proportions hold at
    // every reference size (1.0 at 1920x1080).
    property real layoutScale: 1
    readonly property int avatarSize: referenceLayout ? Math.round(60 * layoutScale) : 50
    readonly property int messageX: referenceLayout ? Math.round(90 * layoutScale) : 68
    readonly property int pipelineIndex: {
        var first = bridge.feed.findIndex(function(entry) { return entry.kind === "jarvis" })
        return first < 0 ? bridge.feed.length - 1 : first
    }
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
    property bool followEnd: true
    onMovementEnded: followEnd = atYEnd
    onCountChanged: { followEnd = true; Qt.callLater(function() { feed.positionViewAtEnd() }) }
    onContentHeightChanged: if (followEnd && !moving) Qt.callLater(function() { feed.positionViewAtEnd() })
    onHeightChanged: if (followEnd && !moving) Qt.callLater(function() { feed.positionViewAtEnd() })

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
        height: Math.max(avatar.height, card.height) + (pipelineLoader.active ? pipelineLoader.height + pipelineLoader.gap : 0)
        Loader {
            id: pipelineLoader
            readonly property int gap: feed.referenceLayout ? 13 : 22
            x: feed.referenceLayout ? card.x + 8 : 68
            y: Math.max(avatar.height, card.height) + gap; width: parent.width - x
            active: feed.pipelineEnabled && index === feed.pipelineIndex
            sourceComponent: Component { StagePipeline { notes: false; reference: feed.referenceLayout } }
        }
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
            width: feed.avatarSize; height: width
            readonly property real k: width / 60
            // You: the operator's initial on a glossy blue sphere with a soft glow.
            Rectangle { visible: row.isUser; anchors.centerIn: parent; width: 78 * avatar.k; height: width; radius: width / 2; color: "#2d7fd0"; opacity: 0.16 }
            Rectangle {
                visible: row.isUser
                anchors.fill: parent; radius: width / 2
                gradient: Gradient { GradientStop { position: 0; color: "#3a86c8" } GradientStop { position: 0.55; color: "#1c5796" } GradientStop { position: 1; color: "#0e3564" } }
                border.width: 1; border.color: "#b36fb6e8"
                Rectangle { anchors { left: parent.left; right: parent.right; top: parent.top; margins: 2 } height: parent.height * 0.46; radius: height
                    gradient: Gradient { GradientStop { position: 0; color: Qt.rgba(1, 1, 1, 0.16) } GradientStop { position: 1; color: "transparent" } } }
                Text { anchors.centerIn: parent; text: bridge.operatorName.charAt(0).toUpperCase(); color: "#f6fbff"; font { family: Theme.font; pixelSize: Math.round(23 * avatar.k); weight: Font.DemiBold } }
            }
            // JARVIS: the luminous ring - a thick bright cyan ring on a dark
            // centre, a soft bloom and a thin calibration ring with nodes.
            Item {
                visible: row.isJarvis
                anchors.fill: parent
                Rectangle { anchors.centerIn: parent; width: 88 * avatar.k; height: width; radius: width / 2; color: Theme.accent; opacity: 0.12 }
                Rectangle { anchors.centerIn: parent; width: 73 * avatar.k; height: width; radius: width / 2; color: "transparent"; border.width: 1; border.color: "#662fc6ee" }
                Repeater { model: 4
                    Rectangle { width: 3; height: 3; radius: 1.5; color: "#7fdcf5"
                        x: avatar.width / 2 + 36.5 * avatar.k * Math.cos((index * 90 + 45) * Math.PI / 180) - 1.5
                        y: avatar.height / 2 + 36.5 * avatar.k * Math.sin((index * 90 + 45) * Math.PI / 180) - 1.5 } }
                Rectangle { anchors.centerIn: parent; width: 66 * avatar.k; height: width; radius: width / 2; color: "transparent"; border.width: 12 * avatar.k; border.color: Theme.accent; opacity: 0.22 }
                Rectangle { anchors.centerIn: parent; width: 56 * avatar.k; height: width; radius: width / 2; color: "#000812"; border.width: Math.max(3, 6.5 * avatar.k); border.color: "#20c9f3" }
                Rectangle { anchors.centerIn: parent; width: 43 * avatar.k; height: width; radius: width / 2; color: "transparent"; border.width: 1.5; border.color: "#b09ff0ff" }
                Rectangle { anchors.centerIn: parent; width: 38 * avatar.k; height: width; radius: width / 2; color: "transparent"; border.width: 4; border.color: "#2620c9f3" }
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
            x: feed.messageX
            readonly property real pad: feed.referenceLayout ? 20 : 21
            width: row.boxed
                   ? (feed.referenceLayout ? Math.min(row.width - x, Math.max(360, row.width * 0.694))
                                           : Math.min(row.width - x - 4, 625, Math.max(360, (row.width - x) * 0.66)))
                   : row.width - x - (feed.referenceLayout ? 0 : 4)
            readonly property real textWidth: row.boxed ? width - 2 * pad : (feed.referenceLayout ? width : Math.min(width - 4, 760))
            height: body.height + (row.boxed ? (feed.referenceLayout ? 31 : 34) : 4)
            radius: 12
            // The request: a compact, clearly bounded deep-navy glass block
            // with a thin cyan perimeter and a top highlight.
            color: row.loud ? Theme.alpha(row.tone, 0.07) : "transparent"
            gradient: row.isUser ? requestGlass : null
            Gradient { id: requestGlass
                GradientStop { position: 0; color: "#f0001a2e" }
                GradientStop { position: 1; color: "#e8001322" } }
            border.width: row.boxed ? 1 : 0
            border.color: row.isUser ? "#2a6a92" : Theme.alpha(row.tone, 0.45)
            Rectangle { visible: row.isUser; x: 14; y: 1; width: parent.width - 28; height: 1; color: "#5a6fd2f5" }
            Rectangle { visible: row.isUser; anchors { left: parent.left; right: parent.right; top: parent.top; margins: 1 } height: Math.min(parent.height * 0.45, 34); radius: 11
                gradient: Gradient { GradientStop { position: 0; color: "#0f4fc3ff" } GradientStop { position: 1; color: "transparent" } } }
            Column {
                id: body
                x: row.boxed ? card.pad : 0; y: row.boxed ? (feed.referenceLayout ? 15 : 16) : 2
                width: card.textWidth
                spacing: row.isJarvis && feed.referenceLayout ? 4 : (feed.referenceLayout ? 7 : 9)
                Row {
                    leftPadding: row.isJarvis && feed.referenceLayout ? 12 : 0
                    spacing: 16
                    // Labels outside a bubble can sit over the planet's bright
                    // horizon: a soft dark outline keeps them legible anywhere.
                    Text { text: feed.kindLabel(row.kind)
                        style: row.isUser ? Text.Normal : Text.Outline; styleColor: "#b3000812"
                        color: row.isUser ? "#f5faff" : row.isJarvis ? "#22c8ee" : row.tone
                        font { family: Theme.font; pixelSize: row.isUser ? (feed.referenceLayout ? 17 : 16) : row.isJarvis ? (feed.referenceLayout ? 18 : 17) : 10
                               weight: row.isJarvis ? Font.Bold : Font.DemiBold
                               letterSpacing: row.isUser ? 0.2 : (row.isJarvis ? 0.6 : 2.2) } }
                    Text { anchors.baseline: parent.children[0].baseline; text: modelData.time; color: "#aac1e2"
                           style: row.isUser ? Text.Normal : Text.Outline; styleColor: "#b3000812"
                           font { family: Theme.font; pixelSize: feed.referenceLayout ? 16 : 15 } }
                }
                Item {
                    width: parent.width
                    height: message.height + (row.isJarvis ? (feed.referenceLayout ? 27 : 24) : 0)
                    // JARVIS: a lighter, quieter glass surface under the answer.
                    Rectangle {
                        visible: row.isJarvis
                        anchors.fill: parent; radius: 12
                        gradient: Gradient {
                            GradientStop { position: 0; color: "#c8001729" }
                            GradientStop { position: 1; color: "#b0001120" }
                        }
                        border.width: 1; border.color: "#1f4a68"
                        Rectangle { x: 14; y: 1; width: parent.width - 28; height: 1; color: "#2e6fd2f5" }
                    }
                    TextEdit {
                        id: message
                        x: row.isJarvis ? (feed.referenceLayout ? 18 : 18) : 0
                        y: row.isJarvis ? (feed.referenceLayout ? 13 : 12) : 0
                        width: feed.referenceLayout ? Math.min(parent.width - 2 * x, row.isJarvis ? 640 : 600) : parent.width - 2 * x
                        text: modelData.text
                        readOnly: true
                        selectByMouse: true
                        wrapMode: Text.Wrap
                        color: row.kind === "system" ? Theme.textMuted : "#f7fcff"
                        selectionColor: Theme.accentSoft
                        font { family: row.kind === "data" ? Theme.mono : Theme.font; pixelSize: row.kind === "data" ? 12 : (feed.referenceLayout ? 20 : 17) }
                        textFormat: TextEdit.PlainText
                    }
                }
            }
        }
    }
}
