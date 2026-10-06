import QtQuick
import "../theme"

// The one attention strip above the composer: "do I need to do something?"
// It shows the highest-priority current item from bridge.safety.attention;
// when there are several, a truthful count and a compact selector step
// through them - priority picks what is shown first, it hides nothing.
// Review brings the matching card into view; it resolves nothing. The strip
// is absent when nothing currently needs attention.
Rectangle {
    id: strip
    objectName: "attentionStrip"
    readonly property var items: bridge.safety.attention || []
    property int index: 0
    readonly property int current: Math.min(index, Math.max(0, items.length - 1))
    readonly property var item: items.length ? items[current] : ({})
    readonly property color tone: item.tone === "danger" ? Theme.danger : item.tone === "warning" ? Theme.warning
                                : item.tone === "unknown" ? Theme.unknownEdge : Theme.accentSoft
    signal review(string key)
    // A changed set of conditions returns the selector to the top item; the
    // same set re-projected (any data refresh) keeps the reader's choice.
    readonly property string keys: items.map(function(i) { return i.key + ":" + i.label }).join("|")
    onKeysChanged: index = 0

    visible: items.length > 0
    height: visible ? 44 : 0
    radius: 10
    color: Theme.glassInteractive
    border.width: 1; border.color: Theme.alpha(tone, 0.6)
    Behavior on border.color { ColorAnimation { duration: 160 } }
    Accessible.role: Accessible.AlertMessage
    Accessible.name: item.label || ""

    // A static leading marker: its shape follows the condition, not colour alone.
    Rectangle {
        id: marker
        x: 14; anchors.verticalCenter: parent.verticalCenter
        width: 22; height: 22; radius: 11; color: Theme.alpha(strip.tone, 0.12); border.width: 1.5; border.color: strip.tone
        Icon { anchors.centerIn: parent; tint: strip.tone; size: 12
               name: ({ execution: "pause", approval: "diamond-filled", decision: "diamond-filled",
                        outcome: strip.item.tone === "unknown" ? "question" : "exclamation" })[strip.item.key] || "exclamation" }
    }
    Text {
        objectName: "attentionLabel"
        anchors { left: marker.right; leftMargin: 12; right: selector.left; rightMargin: 12; verticalCenter: parent.verticalCenter }
        text: strip.item.label || ""; color: strip.tone; elide: Text.ElideRight; font: Theme.uiValue
    }
    Row {
        id: selector
        anchors { right: reviewButton.left; rightMargin: 6; verticalCenter: parent.verticalCenter }
        spacing: 2
        visible: strip.items.length > 1
        width: visible ? implicitWidth : 0
        IconButton { objectName: "attentionPrev"; icon: "chevron-left"; tip: qsTr("Previous item"); width: 30; height: 30; enabled: strip.current > 0; onClicked: strip.index = strip.current - 1 }
        Text { objectName: "attentionCount"; anchors.verticalCenter: parent.verticalCenter; width: 48; horizontalAlignment: Text.AlignHCenter
               text: qsTr("%1 of %2").arg(strip.current + 1).arg(strip.items.length); color: Theme.textLabel; font: Theme.metadata }
        IconButton { objectName: "attentionNext"; icon: "chevron-right"; tip: qsTr("Next item"); width: 30; height: 30; enabled: strip.current < strip.items.length - 1; onClicked: strip.index = strip.current + 1 }
    }
    ActionButton {
        id: reviewButton
        objectName: "attentionReview"
        anchors { right: parent.right; rightMargin: 6; verticalCenter: parent.verticalCenter }
        height: 34; compact: true
        label: qsTr("Review") + "  ›"
        onClicked: strip.review(strip.item.key)
    }
}
