import QtQuick
import "../theme"

// The conversation stream. JARVIS answers sit on the LEFT: the luminous ring
// avatar, the name, the answer on its quieter glass surface, the time
// beneath. The operator's turns sit on the RIGHT in bounded deep-navy
// technical bubbles, the time beneath. System, error, unknown and approval
// entries stay on the left in their own colour. Long content stays readable
// and selectable; nothing is invented for the stream.
//
// Scrolling is a Flickable over one Column: every entry is laid out, so the
// content height, the thumb size and the thumb travel are exact (a ListView
// estimates them from the few delegates it has and drifts). Reading
// position is the reader's: following the end is what happens while they
// are at the end; once they scroll up to read older messages, a new entry,
// a composer resize or any relayout leaves them exactly where they are.
//
// The scrollbar is transient: invisible at rest, shown at once on any
// scroll, 2 px while scrolling and 3 px under the pointer or in a drag, held
// one second after the last interaction and faded in 200 ms. Its 16 px
// interaction strip is always reserved, so messages never shift when it
// appears; no overflow, no scrollbar.
Item {
    id: feed
    property bool pipelineEnabled: false
    property bool referenceLayout: false
    // The avatar column scales with the surface (1.0 at the 1920x1080 grid).
    property real layoutScale: 1
    readonly property int avatarSize: referenceLayout ? 36 : 50
    readonly property int messageX: referenceLayout ? avatarSize + 12 : 68
    readonly property int strip: 16
    readonly property int pipelineIndex: {
        var first = bridge.feed.findIndex(function(entry) { return entry.kind === "jarvis" })
        return first < 0 ? bridge.feed.length - 1 : first
    }
    // For inspection: the scroll state.
    readonly property alias contentY: flick.contentY
    readonly property alias contentHeight: flick.contentHeight
    readonly property alias viewportHeight: flick.height
    property alias followEnd: flick.followEnd
    readonly property bool overflow: flick.contentHeight > flick.height + 1
    function scrollTo(y) { flick.contentY = Math.max(0, Math.min(flick.endY, y)) }

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

    Flickable {
        id: flick
        anchors.fill: parent
        clip: true
        contentWidth: width
        contentHeight: stack.height
        flickableDirection: Flickable.VerticalFlick
        boundsBehavior: Flickable.StopAtBounds
        maximumFlickVelocity: 2600
        readonly property real endY: Math.max(0, contentHeight - height)
        // True while the reader is at the end; updated from every change the
        // reader makes (wheel, flick, drag, thumb), never from our own settling.
        property bool followEnd: true
        property bool settling: false
        function remember() { followEnd = contentY >= endY - 6 }
        onContentYChanged: if (!settling) remember()
        // Content grew or the viewport changed: keep the end if the reader
        // was there, otherwise leave the reading position exactly as it is
        // (only pulling back inside the range when the range shrank).
        function settle() {
            settling = true
            if (followEnd) contentY = endY
            else if (contentY > endY) contentY = endY
            settling = false
        }
        onContentHeightChanged: Qt.callLater(settle)
        onHeightChanged: Qt.callLater(settle)
        Component.onCompleted: Qt.callLater(settle)

        Column {
            id: stack
            width: flick.width - feed.strip
            spacing: feed.referenceLayout ? 18 : 16
            Repeater {
                model: bridge.feed
                delegate: Item {
                    id: row
                    objectName: "feedRow_" + kind
                    width: stack.width
                    height: Math.max(row.isUser ? 0 : avatar.height, card.height + stamp.height + stamp.gap)
                            + (pipelineLoader.active ? pipelineLoader.height + pipelineLoader.gap : 0)
                    readonly property string kind: modelData.kind
                    readonly property bool isUser: kind === "user"
                    readonly property bool isJarvis: kind === "jarvis" || kind === "clarify"
                    readonly property bool loud: kind === "error" || kind === "unknown" || kind === "approval"
                    readonly property color tone: feed.railColor(kind)
                    // Appearance: fade + short slide, no bounce.
                    opacity: 0
                    transform: Translate { id: slide; y: 8 }
                    Component.onCompleted: appear.start()
                    ParallelAnimation {
                        id: appear
                        NumberAnimation { target: row; property: "opacity"; to: 1; duration: Theme.panelIn; easing.type: Easing.OutCubic }
                        NumberAnimation { target: slide; property: "y"; to: 0; duration: Theme.panelIn; easing.type: Easing.OutCubic }
                    }
                    // The existing four-stage lifecycle rides with the first JARVIS
                    // turn (or the last entry before one): real state, never a plan.
                    Loader {
                        id: pipelineLoader
                        readonly property int gap: feed.referenceLayout ? 12 : 22
                        x: row.isUser ? 0 : card.x
                        y: Math.max(row.isUser ? 0 : avatar.height, card.height + stamp.height + stamp.gap) + gap
                        width: parent.width - x
                        active: feed.pipelineEnabled && index === feed.pipelineIndex
                        sourceComponent: Component { StagePipeline { notes: false; reference: feed.referenceLayout } }
                    }

                    // -- avatar column (JARVIS and the state markers; not the operator) --
                    Item {
                        id: avatar
                        visible: !row.isUser
                        width: feed.avatarSize; height: width
                        readonly property real k: width / 60
                        // JARVIS: the simplified Reactor-derived identity mark - a dark
                        // circular housing, one controlled blue annulus with three
                        // subdued radial cuts, a dark optical centre. 32 px, minimal
                        // bloom, inside a 36 px reserve so nothing clips.
                        Item {
                            objectName: row.isJarvis ? "feedAvatar_jarvis" : ""
                            visible: row.isJarvis
                            anchors.centerIn: parent; width: 32; height: 32
                            Rectangle { anchors.centerIn: parent; width: 36; height: 36; radius: 18; color: Theme.alpha("#36aaff", 0.09) }
                            Rectangle { anchors.fill: parent; radius: 16; color: "#061424"; border.width: 1; border.color: "#2a5f8f" }
                            Rectangle { anchors.centerIn: parent; width: 22; height: 22; radius: 11; color: "transparent"; border.width: 4; border.color: "#3aa8f0" }
                            Repeater { model: 3
                                Rectangle { width: 1.5; height: 8; color: "#061424"; x: 16 - 0.75; y: 16 - 11.5
                                    transform: Rotation { origin.x: 0.75; origin.y: 11.5; angle: 90 + index * 120 } } }
                            Rectangle { anchors.centerIn: parent; width: 12; height: 12; radius: 6; color: "#03101c" }
                        }
                        // Everything else: a state marker on the rail.
                        Rectangle {
                            visible: !row.isJarvis
                            anchors.centerIn: parent; width: 30; height: 30; radius: 15
                            color: Theme.alpha(row.tone, 0.12); border.width: 1; border.color: Theme.alpha(row.tone, 0.5)
                            Rectangle { anchors.centerIn: parent; width: 8; height: 8; radius: 4; color: row.tone }
                        }
                    }

                    // The operator's bubble is content-sized: measured unwrapped here,
                    // bounded to the right part of the stream.
                    Text { id: probe; visible: false; text: modelData.text; font: message.font }

                    // -- body --------------------------------------------------------------
                    Rectangle {
                        id: card
                        objectName: "feedCard_" + row.kind
                        readonly property real pad: feed.referenceLayout ? 18 : 20
                        readonly property real maxUser: Math.min(720, Math.max(280, row.width * 0.62))
                        x: row.isUser ? row.width - width : feed.messageX
                        width: row.isUser ? Math.min(maxUser, Math.ceil(probe.implicitWidth) + 2 * pad + 2)
                             : row.loud ? Math.min(row.width - x, Math.max(360, row.width * 0.694))
                             : row.width - x
                        readonly property real textWidth: (row.isUser || row.loud) ? width - 2 * pad : width
                        height: body.height + (row.isUser ? 2 * (feed.referenceLayout ? 13 : 14) : row.loud ? (feed.referenceLayout ? 31 : 34) : 4)
                        radius: 12
                        // The request: a compact, clearly bounded deep-navy technical
                        // block with a thin cool-blue perimeter and a top highlight.
                        color: row.loud ? Theme.glassPrimaryReading : "transparent"
                        gradient: row.isUser ? requestGlass : null
                        Gradient { id: requestGlass
                            GradientStop { position: 0; color: Theme.glassUserReading }
                            GradientStop { position: 1; color: Theme.glassPrimaryReading } }
                        border.width: row.isUser || row.loud ? 1 : 0
                        border.color: row.isUser ? "#327bb5" : Theme.alpha(row.tone, 0.45)
                        Rectangle { visible: row.isUser; x: 14; y: 1; width: parent.width - 28; height: 1; color: "#5a6fd2f5" }
                        Rectangle { visible: row.isUser; anchors { left: parent.left; right: parent.right; top: parent.top; margins: 1 } height: Math.min(parent.height * 0.45, 34); radius: 11
                            gradient: Gradient { GradientStop { position: 0; color: "#0f4fc3ff" } GradientStop { position: 1; color: "transparent" } } }
                        Column {
                            id: body
                            x: row.isUser || row.loud ? card.pad : 0
                            y: row.isUser ? (feed.referenceLayout ? 13 : 14) : row.loud ? (feed.referenceLayout ? 15 : 16) : 2
                            width: card.textWidth
                            spacing: row.isJarvis ? 0 : (feed.referenceLayout ? 7 : 9)
                            // The label line: JARVIS carries its name inside its surface;
                            // the operator's bubble needs none; the rest are labelled.
                            Text { visible: row.loud || row.kind === "system" || row.kind === "scheduled" || row.kind === "data"
                                   text: feed.kindLabel(row.kind)
                                   style: Text.Outline; styleColor: "#b3000812"
                                   color: row.tone
                                   font: feed.referenceLayout ? Theme.tracked(Theme.messageIdentity, 0.72) : Qt.font({ family: Theme.font, pixelSize: 10, weight: Font.DemiBold, letterSpacing: 2.2 }) }
                            Item {
                                width: parent.width
                                height: message.y + message.height + (row.isJarvis ? (feed.referenceLayout ? 13 : 12) : 0)
                                // JARVIS: a lighter, quieter glass surface under the answer.
                                Rectangle {
                                    visible: row.isJarvis
                                    anchors.fill: parent; radius: 12
                                    color: Theme.glassPrimaryReading
                                    border.width: 1; border.color: Theme.glassSeparator
                                    Rectangle { x: 14; y: 1; width: parent.width - 28; height: 1; color: "#2e6fd2f5" }
                                }
                                Text { id: name; visible: row.isJarvis; x: 18; y: 11; text: feed.kindLabel(row.kind); color: Theme.materialBlue
                                       font: feed.referenceLayout ? Theme.messageIdentity : Qt.font({ family: Theme.font, pixelSize: 12, weight: Font.Bold, letterSpacing: 1.4 })
                                       lineHeightMode: Text.FixedHeight; lineHeight: 16 }
                                TextEdit {
                                    id: message
                                    x: row.isJarvis ? 18 : 0
                                    y: row.isJarvis ? name.y + name.height + 6 : 0
                                    width: row.isJarvis ? Math.min(parent.width - 2 * x, 760) : parent.width
                                    text: modelData.text
                                    readOnly: true
                                    selectByMouse: true
                                    wrapMode: Text.Wrap
                                    objectName: "feedMessage_" + row.kind
                                    color: row.kind === "system" ? Theme.textMuted : Theme.text
                                    selectionColor: Theme.accentSoft
                                    font: row.kind === "data" ? Qt.font({ family: Theme.mono, pixelSize: 12 })
                                          : feed.referenceLayout ? Theme.conversationBody : Qt.font({ family: Theme.font, pixelSize: 16 })
                                    textFormat: TextEdit.PlainText
                                    // 18 / 28 reading rhythm, paragraphs ~12 px apart (TextEdit has no lineHeight).
                                    // Applied once: a feed entry's text never changes after the row exists
                                    // (a new feed rebuilds the rows), and the formatting edit itself would
                                    // otherwise re-trigger a text-change handler.
                                    Component.onCompleted: if (feed.referenceLayout && row.kind !== "data" && typeof typography !== "undefined") typography.setLineHeight(textDocument, 28, 12)
                                }
                            }
                        }
                    }
                    // The time, beneath the turn: at the right under the operator's
                    // bubble, at the left under JARVIS; a soft outline keeps it
                    // legible over the planet's bright horizon.
                    Text {
                        id: stamp
                        readonly property int gap: 5
                        x: row.isUser ? row.width - width - 6 : card.x + (row.isJarvis ? 18 : 0)
                        y: card.height + gap
                        text: modelData.time; color: Theme.textMuted
                        style: Text.Outline; styleColor: "#b3000812"
                        font: feed.referenceLayout ? Theme.timestamp : Qt.font({ family: Theme.font, pixelSize: 11 })
                        lineHeightMode: Text.FixedHeight; lineHeight: 16
                    }
                }
            }
        }
    }

    // -- the transient scrollbar, in the reserved strip -------------------------
    Item {
        id: bar
        objectName: "feedScrollBar"
        x: feed.width - feed.strip; y: 0; width: feed.strip; height: feed.height
        visible: feed.overflow
        readonly property real thumbH: Math.max(24, Math.round(height * flick.height / Math.max(1, flick.contentHeight)))
        readonly property real travel: Math.max(0, height - thumbH)
        readonly property bool engaged: hit.pressed || hit.containsMouse
        property bool recent: false
        readonly property bool shown: engaged || flick.moving || recent
        Timer { id: hold; interval: 1000; onTriggered: bar.recent = false }
        function poke() { recent = true; hold.restart() }
        // Any scroll the reader makes shows the bar at once; when the pointer
        // leaves or the drag ends, hold, then fade.
        Connections { target: flick; function onContentYChanged() { if (!flick.settling) bar.poke() } }
        onEngagedChanged: if (!engaged) poke()
        opacity: 0
        onShownChanged: { if (shown) { fadeOut.stop(); opacity = 1 } else fadeOut.restart() }
        NumberAnimation { id: fadeOut; target: bar; property: "opacity"; to: 0; duration: 200 }
        Rectangle {
            id: thumb
            objectName: "feedScrollThumb"
            anchors.right: parent.right; anchors.rightMargin: 6
            width: bar.engaged ? 3 : 2
            height: bar.thumbH
            y: flick.endY > 0 ? bar.travel * Math.min(1, Math.max(0, flick.contentY / flick.endY)) : 0
            radius: 1.5
            color: bar.engaged ? "#5cc0f0" : "#3a9fdb"
            Behavior on width { NumberAnimation { duration: 100 } }
        }
        MouseArea {
            id: hit
            anchors.fill: parent
            hoverEnabled: true
            preventStealing: true
            property real grab: 0
            function moveTo(py) {
                var t = Math.min(1, Math.max(0, (py - grab) / Math.max(1, bar.travel)))
                flick.contentY = t * flick.endY
            }
            onPressed: function(mouse) {
                var onThumb = mouse.y >= thumb.y && mouse.y <= thumb.y + thumb.height
                grab = onThumb ? mouse.y - thumb.y : thumb.height / 2
                if (!onThumb) moveTo(mouse.y)
            }
            onPositionChanged: function(mouse) { if (pressed) moveTo(mouse.y) }
        }
    }
}
