import QtQuick

// The status ring: six states, drawn only from `mood`. Clicking it is the window's size toggle.
Item {
    id: ring
    property string mood: "idle"
    readonly property var moods: ["idle", "listening", "understanding", "thinking", "speaking", "needs_you"]
    readonly property var labels: ({
        idle: qsTr("Boşta"), listening: qsTr("Dinliyor"), understanding: qsTr("Anlıyor"),
        thinking: qsTr("Düşünüyor"), speaking: qsTr("Konuşuyor"), needs_you: qsTr("Seni bekliyor")
    })
    readonly property var tints: ({
        idle: "#5b6b82", listening: "#3fa7ff", understanding: "#7c8cff",
        thinking: "#b48cff", speaking: "#38d39f", needs_you: "#ffb347"
    })
    readonly property color tint: tints[mood] || tints.idle
    readonly property string label: labels[mood] || labels.idle
    signal clicked()

    implicitWidth: 56; implicitHeight: 56
    Accessible.role: Accessible.Button
    Accessible.name: qsTr("Durum halkası: %1").arg(label)

    Rectangle {                                     // outer ring
        id: outer
        anchors.centerIn: parent
        width: Math.min(ring.width, ring.height); height: width; radius: width / 2
        color: "transparent"
        border.width: Math.max(3, width * 0.08); border.color: ring.tint
        Behavior on border.color { ColorAnimation { duration: 200 } }
        // thinking / understanding: a dashed arc that turns
        Rectangle {
            visible: ring.mood === "thinking" || ring.mood === "understanding"
            width: parent.width * 0.22; height: parent.border.width; radius: height / 2
            color: "white"; opacity: 0.85
            x: parent.width / 2 - width / 2; y: 0
        }
        RotationAnimator on rotation {
            running: ring.mood === "thinking" || ring.mood === "understanding"
            from: 0; to: 360; duration: ring.mood === "thinking" ? 1400 : 2600; loops: Animation.Infinite
        }
    }
    Rectangle {                                     // core: pulses while listening / speaking / waiting for you
        id: core
        anchors.centerIn: parent
        width: outer.width * 0.42; height: width; radius: width / 2
        color: ring.tint; opacity: 0.9
        Behavior on color { ColorAnimation { duration: 200 } }
        SequentialAnimation on scale {
            running: ring.mood === "listening" || ring.mood === "speaking" || ring.mood === "needs_you"
            loops: Animation.Infinite
            NumberAnimation { to: 1.25; duration: ring.mood === "speaking" ? 260 : 600; easing.type: Easing.InOutSine }
            NumberAnimation { to: 1.0; duration: ring.mood === "speaking" ? 260 : 600; easing.type: Easing.InOutSine }
        }
    }
    MouseArea {
        anchors.fill: parent
        cursorShape: Qt.PointingHandCursor
        onClicked: ring.clicked()
    }
}
