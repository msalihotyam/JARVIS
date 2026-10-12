import QtQuick
import "../theme"

// Gate 6G R3 (D1/D2): a conversation row's 6 px status dot, from the bridge's
// real runtime state only (Sidebar reads runningIds / needsYouIds /
// finishedUnseenIds). Idle: an unlit, barely visible ring. "running": violet,
// blinking. "needsYou": amber and steady (approval attention never moves,
// K2). "finished": green until opened; `glow` drives the row's soft green
// pulse (D2). With ambient effects off the colours stay and nothing blinks or
// pulses (K2). Its hover tip says the state in words (K3).
Item {
    id: dot
    property string status: ""
    readonly property string words: status === "running" ? qsTr("Running")
                                    : status === "needsYou" ? qsTr("Waiting for you")
                                    : status === "queued" ? qsTr("Queued")
                                    : status === "finished" ? qsTr("Finished") : ""
    // DFQUEUE "queued": a message waits there for the one run at a time - accent, steady.
    readonly property color tone: status === "running" ? Theme.violet
                                  : status === "needsYou" ? Theme.warning
                                  : status === "queued" ? Theme.accent : Theme.success
    readonly property bool blinking: status === "running" && Theme.ambient && visible
    readonly property bool pulsing: status === "finished" && Theme.ambient && visible
    readonly property bool animated: blinking || pulsing
    readonly property real glow: pulsing ? pulse : 1
    readonly property bool hovered: zone.hovered
    property real blink: 1
    property real pulse: 1
    width: 6; height: 6
    Accessible.role: Accessible.Indicator
    Accessible.name: words

    Rectangle {
        objectName: "statusDotFill"
        anchors.fill: parent; radius: width / 2
        color: dot.status === "" ? "transparent" : dot.tone
        border.width: dot.status === "" ? 1 : 0
        border.color: Theme.alpha(Theme.textLabel, 0.35)
        opacity: dot.blinking ? dot.blink : 1
    }
    // Rhythms of a real state, only while it holds (never with ambient off).
    SequentialAnimation on blink {
        running: dot.blinking
        loops: Animation.Infinite
        NumberAnimation { to: 0.25; duration: Theme.dur(700); easing.type: Easing.InOutSine }
        NumberAnimation { to: 1; duration: Theme.dur(700); easing.type: Easing.InOutSine }
    }
    SequentialAnimation on pulse {
        running: dot.pulsing
        loops: Animation.Infinite
        NumberAnimation { to: 0.35; duration: Theme.dur(1800); easing.type: Easing.InOutSine }
        NumberAnimation { to: 1; duration: Theme.dur(1800); easing.type: Easing.InOutSine }
    }
    // A hover zone a little larger than the dot, so its tip can be reached.
    Item {
        anchors.centerIn: parent; width: 14; height: 18
        HoverHandler { id: zone }
        HoverTip { objectName: "statusDotTip"; text: dot.words }
    }
}
