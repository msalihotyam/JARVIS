import QtQuick
import "../theme"
import "../components"

// Canonical/Dark home: the conversation surface, reconstructed against the
// approved conversation helper. The Phase 1 outer bounds are fixed; inside
// them every inset is a fraction of the work-surface width, taken from the
// helper at its own scale (its panel maps onto the 1096 px surface of the
// 1920x1080 layout at 0.812):
//   greeting 5.0 %, avatar centre 8.2 %, content 13.6 %,
//   request 62 %, answer / workflow to 95 %, composer 94 % centred.
GlassPanel {
    id: page
    objectName: "canonicalWorkspace"
    level: 1; padding: 18; brackets: true
    // 1.0 at the 1920x1080 reference; every inset below scales with it.
    readonly property real sx: Math.max(0.6, width / 1096)
    function at(v) { return Math.round(v * sx) }
    readonly property bool roomy: width >= 700

    // Canonical Space: a deep blue-black glass over its own local
    // atmosphere (a static decorative plate - no text, no data). The frame
    // takes the helper's luminous cyan edge. Dark keeps its quieter panel.
    color: Theme.space ? "#f2000812" : Theme.surfaceWorkspace
    border.color: Theme.space ? "#b33b83c6" : Theme.border
    Image {
        parent: page
        z: -0.5
        anchors.fill: parent; anchors.margins: 1
        source: "../assets/conversation-atmosphere.png"
        fillMode: Image.PreserveAspectCrop
        horizontalAlignment: Image.AlignLeft; verticalAlignment: Image.AlignBottom
        smooth: true; asynchronous: true; cache: true
        opacity: Theme.space ? 1 : 0
        visible: opacity > 0
        Behavior on opacity { NumberAnimation { duration: Theme.themeMs } }
    }

    Column {
        id: heading
        anchors { left: parent.left; right: parent.right; top: parent.top; leftMargin: page.roomy ? page.at(37) : 12; topMargin: page.at(15) }
        spacing: page.at(6)
        Text { text: (function() { var h = new Date().getHours(); return (h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening") + ", " + bridge.operatorName + "." })()
               color: "#f4faff"; font { family: Theme.font; pixelSize: page.roomy ? Math.max(27, page.at(33)) : 26; weight: Font.DemiBold } }
        Text { width: parent.width; wrapMode: Text.Wrap
               text: bridge.configRefused ? "Configuration refused — execution is off." : !bridge.executionEnabled ? "Execution is disabled. Re-enable it explicitly when ready." : "Here to help you think, plan, and get things done."
               color: bridge.configRefused || !bridge.executionEnabled ? Theme.danger : "#b8ccef"
               font { family: Theme.font; pixelSize: page.roomy ? 18 : 16 } }
    }
    Item {
        id: flow
        anchors { left: parent.left; right: parent.right; top: heading.bottom; bottom: choices.top
                  leftMargin: page.roomy ? page.at(41) : 8; rightMargin: page.roomy ? page.at(33) : 8
                  topMargin: page.at(14); bottomMargin: page.at(10) }
        Item { id: ready; visible: bridge.feed.length === 0; width: parent.width; height: 92
            IconBox { glyph: "◎"; width: 60; height: 60; tint: Theme.accent; round: true }
            Column { x: 90; y: 6; width: parent.width - 90; spacing: 8
                Text { text: "JARVIS"; color: Theme.materialBlue; font { family: Theme.font; pixelSize: 17; letterSpacing: 0.5; weight: Font.Bold } }
                Text { width: parent.width; wrapMode: Text.Wrap; text: bridge.configRefused ? "Configuration needs attention before any work can start." : !bridge.executionEnabled ? "Standing by. No new work will start while execution is disabled." : "Waiting for your request. I’ll help you plan the next step and ask before an action needs your permission."; color: "#f3f9ff"; font { family: Theme.font; pixelSize: 18 } }
            }
        }
        FeedView {
            objectName: "conversationFeed"
            anchors { left: parent.left; right: parent.right; bottom: parent.bottom; top: ready.visible ? ready.bottom : parent.top; topMargin: ready.visible ? 18 : 0 }
            pipelineEnabled: true
            referenceLayout: page.roomy
            layoutScale: Math.max(0.75, Math.min(1, page.sx))
            footer: Component { Item { width: ListView.view ? ListView.view.width : flow.width; height: bridge.feed.length === 0 ? emptyPipeline.height + 8 : 0
                StagePipeline { id: emptyPipeline; x: Math.round(98 * Math.max(0.75, Math.min(1, page.sx))); width: parent.width - x; reference: page.roomy; visible: bridge.feed.length === 0 }
            } }
        }
    }
    Column {
        id: choices
        anchors { left: parent.left; right: parent.right; bottom: composer.top; leftMargin: flow.anchors.leftMargin + 90; rightMargin: 36; bottomMargin: visible ? 18 : 0 }
        visible: !!bridge.clarification.choices && bridge.clarification.choices.length > 0
        height: visible ? implicitHeight : 0; spacing: 10
        Text { text: "CHOOSE ONE"; color: Theme.accentSoft; font { family: Theme.font; pixelSize: 10; letterSpacing: 2 } }
        Flow { width: parent.width; spacing: 10
            Repeater { model: bridge.clarification.choices || []; ActionButton { label: modelData.label; enabled: !bridge.busy; onClicked: bridge.chooseOption(modelData.label) } }
            ActionButton { label: "Never mind"; onClicked: bridge.dismissClarification() }
        }
    }
    // The composer: 94 % of the surface, centred, the surface's bottom anchor.
    Composer { id: composer; objectName: "canonicalComposer"; height: page.roomy ? 79 : 66
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom
                  leftMargin: page.roomy ? page.at(15) : 6; rightMargin: page.roomy ? page.at(15) : 6; bottomMargin: page.roomy ? -page.at(3) : 6 }
    }
}
