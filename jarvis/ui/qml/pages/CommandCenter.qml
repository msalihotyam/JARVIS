import QtQuick
import "../theme"
import "../components"

// Canonical/Dark home: the conversation-first work surface. Its shell rises
// `lift` px above the panel line the right stack starts from, into the
// header environment: the material fades in over the first `fade` px - no
// top border, nothing bright under the reactor - and the text begins below
// the fade. The reactor is not moved and the header does not grow.
//
// A genuinely empty conversation shows one lightweight welcome and nothing
// else: no sample transcript, no invented workflow. The first entry the
// application accepts replaces it, and the whole surface is the stream.
Item {
    id: page
    objectName: "canonicalWorkspace"
    property int lift: 37       // the material band: fully present at the panel line
    property int fade: 48       // where the text begins, below the band
    readonly property bool roomy: width >= 700
    readonly property bool empty: bridge.feed.length === 0 && !bridge.safety.cards
    // Primary content glass: the shell lets the space through in its unused
    // areas; every message and structured block sits on its own darker
    // reading island (FeedView).
    readonly property color fill: Theme.glassPrimaryShell
    readonly property color edge: Theme.glassEdge
    readonly property int shellRadius: Theme.radius
    readonly property int inset: roomy ? 16 : 8

    // -- the shell material: plain rectangles, no effects -----------------------
    // The fill appears over the fade band, then continues as a square-topped
    // body; the side edges fade in with it; the bottom edge and its two
    // corners are the lower half of a frame, clipped.
    Rectangle {
        x: 1; y: 0; width: parent.width - 2; height: page.lift + 1
        gradient: Gradient { GradientStop { position: 0; color: "transparent" } GradientStop { position: 1; color: page.fill } }
    }
    Rectangle {
        x: 0; y: page.lift; width: parent.width; height: parent.height - y
        color: page.fill; radius: page.shellRadius; topLeftRadius: 0; topRightRadius: 0
        // Material: a faint foot, as the other glass has.
        Rectangle { anchors { left: parent.left; right: parent.right; bottom: parent.bottom; margins: 1 } height: Math.min(parent.height * 0.3, 140); radius: parent.radius - 1
            gradient: Gradient { GradientStop { position: 0; color: "transparent" } GradientStop { position: 1; color: Qt.rgba(0, 0, 0, 0.16) } } }
    }
    Repeater {
        model: 2
        Rectangle {
            x: index === 0 ? 0 : page.width - 1; y: 0; width: 1; height: page.height - page.shellRadius
            gradient: Gradient {
                GradientStop { position: 0; color: "transparent" }
                GradientStop { position: Math.min(0.999, page.lift / Math.max(1, page.height - page.shellRadius)); color: page.edge }
                GradientStop { position: 1; color: page.edge }
            }
        }
    }
    Item {
        x: 0; y: page.height - page.shellRadius - 1; width: page.width; height: page.shellRadius + 1; clip: true
        Rectangle { x: 0; y: -page.shellRadius - 1; width: parent.width; height: 2 * page.shellRadius + 2; radius: page.shellRadius
            color: "transparent"; border.width: 1; border.color: page.edge }
    }

    // -- the stream, below the fade --------------------------------------------
    Item {
        id: flowArea
        anchors { left: parent.left; right: parent.right; top: parent.top; bottom: attention.top
                  topMargin: page.fade; leftMargin: page.inset + (page.roomy ? 12 : 4); rightMargin: page.inset + (page.roomy ? 4 : 0); bottomMargin: 10 }
        FeedView {
            id: conversation
            objectName: "conversationFeed"
            safetyCards: true
            anchors.fill: parent
            visible: !page.empty
            referenceLayout: page.roomy
            layoutScale: Math.max(0.75, Math.min(1, page.width / 1231))
        }
        // The welcome: the greeting and one real question. It carries the
        // truthful execution state instead when that state needs attention.
        Column {
            id: welcome
            objectName: "conversationWelcome"
            visible: page.empty
            anchors.centerIn: parent
            anchors.verticalCenterOffset: -Math.round(parent.height * 0.06)
            width: Math.min(parent.width - 40, 760)
            spacing: page.roomy ? 8 : 6
            readonly property int tier: Theme.tier(page.Window.width)
            Text { objectName: "welcomeGreeting"; width: parent.width; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap
                   text: (function() { var h = new Date().getHours(); return (h < 12 ? qsTr("Good morning, %1.") : h < 18 ? qsTr("Good afternoon, %1.") : qsTr("Good evening, %1.")).arg(bridge.operatorName) })()
                   color: Theme.text; font: Theme.greeting(welcome.tier)
                   lineHeightMode: Text.FixedHeight; lineHeight: Theme.greetingLine(welcome.tier) }
            Text { objectName: "welcomeSupport"; width: parent.width; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap
                   text: bridge.configRefused ? qsTr("Configuration refused — execution is off.") : !bridge.executionEnabled ? qsTr("JARVIS is frozen. Resume it from the red control or the tray when ready.") : qsTr("What would you like to work on?")
                   color: bridge.configRefused || !bridge.executionEnabled ? Theme.danger : Theme.textLabel
                   font: Theme.greetingSupport(welcome.tier)
                   lineHeightMode: Text.FixedHeight; lineHeight: Theme.greetingSupportLine(welcome.tier) }
        }
    }
    // The one attention strip, immediately above the choices and the composer.
    AttentionStrip {
        id: attention
        anchors { left: parent.left; right: parent.right; bottom: choices.top
                  leftMargin: page.inset; rightMargin: page.inset; bottomMargin: visible ? 10 : 0 }
        onReview: function(key) { conversation.reveal(key) }
    }
    Column {
        id: choices
        anchors { left: parent.left; right: parent.right; bottom: composer.top; leftMargin: flowArea.anchors.leftMargin + 68; rightMargin: 36; bottomMargin: visible ? 18 : 0 }
        visible: !!bridge.clarification.choices && bridge.clarification.choices.length > 0
        height: visible ? implicitHeight : 0; spacing: 10
        Text { text: qsTr("CHOOSE ONE"); color: Theme.accentSoft; font: Theme.label }
        Flow { width: parent.width; spacing: 10
            Repeater { model: bridge.clarification.choices || []; ActionButton { label: modelData.label; enabled: !bridge.busy; onClicked: bridge.chooseOption(modelData.label) } }
            ActionButton { label: qsTr("Never mind"); onClicked: bridge.dismissClarification() }
        }
    }
    // The composer: the surface's bottom anchor, growing upward with a draft
    // to the smaller of 180 px and a quarter of the surface.
    Composer { id: composer; objectName: "canonicalComposer"
        maxHeight: Math.min(180, Math.round(page.height * 0.25))
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom
                  leftMargin: page.inset; rightMargin: page.inset; bottomMargin: 12 }
    }
}
