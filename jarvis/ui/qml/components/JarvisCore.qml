import QtQuick
import "../theme"

// The JARVIS core: identity and live state in one object. Rings rotate
// (slowly when idle, faster when running), the glow breathes with the
// state, and the accent color is the state color. Transforms and opacity
// only - no particles, no shaders.
Item {
    id: core
    property string state_: "idle"
    property bool active: true
    property color stateColor: Theme.stateColor(state_)
    readonly property bool running: state_ === "running"
    readonly property bool dormant: state_ === "disabled"
    readonly property bool ambientMotion: Theme.ambient && active && !dormant
    implicitWidth: 150; implicitHeight: 150

    Behavior on stateColor { ColorAnimation { duration: 260; easing.type: Easing.InOutCubic } }

    // Glow: layered circles fade with strength; cheap and static per state.
    Repeater {
        model: 4
        Rectangle {
            anchors.centerIn: parent
            width: core.width * (0.72 + index * 0.16); height: width; radius: width / 2
            color: core.stateColor
            opacity: (dormant ? 0.02 : 0.10 - index * 0.02) * glow.energy * Theme.glowStrength
        }
    }
    QtObject {
        id: glow
        property real energy: 1.0
        SequentialAnimation on energy {
            running: core.ambientMotion
            loops: Animation.Infinite
            NumberAnimation { to: running ? 1.6 : (state_ === "awaiting_approval" ? 1.5 : 1.2)
                              duration: running ? 900 : 2400; easing.type: Easing.InOutSine }
            NumberAnimation { to: 1.0; duration: running ? 900 : 2400; easing.type: Easing.InOutSine }
        }
    }

    // Outer ring with tick marks, rotating.
    Item {
        id: outer
        anchors.centerIn: parent
        width: core.width * 0.96; height: width
        RotationAnimator on rotation {
            running: core.ambientMotion || core.running
            loops: Animation.Infinite
            from: 0; to: 360
            duration: core.running ? 9000 : 42000
        }
        Rectangle {
            anchors.fill: parent; radius: width / 2; color: "transparent"
            border.width: 1; border.color: core.stateColor; opacity: dormant ? 0.25 : 0.55
        }
        Repeater {
            model: 24
            Rectangle {
                width: index % 6 === 0 ? 2 : 1; height: index % 6 === 0 ? 10 : 5
                color: core.stateColor
                opacity: dormant ? 0.2 : 0.7
                x: outer.width / 2 - width / 2; y: 0
                transform: Rotation { origin.x: 1; origin.y: outer.height / 2; angle: index * 15 }
            }
        }
    }

    // Middle broken ring, counter-rotating.
    Item {
        id: middle
        anchors.centerIn: parent
        width: core.width * 0.74; height: width
        RotationAnimator on rotation {
            running: core.ambientMotion || core.running
            loops: Animation.Infinite
            from: 360; to: 0
            duration: core.running ? 6000 : 30000
        }
        Repeater {
            model: 3
            Rectangle {
                anchors.fill: parent; radius: width / 2; color: "transparent"
                border.width: 2; border.color: core.stateColor
                opacity: dormant ? 0.15 : 0.35
                rotation: index * 120
                // A gap: cover a third of the ring with the background color.
                Rectangle {
                    width: parent.width * 0.42; height: parent.height * 0.42
                    x: parent.width * 0.29; y: -4
                    color: "transparent"
                }
            }
        }
        Rectangle {
            anchors.fill: parent; radius: width / 2; color: "transparent"
            border.width: 2; border.color: core.stateColor; opacity: dormant ? 0.15 : 0.35
        }
    }

    // Inner planet: the identity.
    Rectangle {
        id: planet
        anchors.centerIn: parent
        width: core.width * 0.5; height: width; radius: width / 2
        gradient: Gradient {
            orientation: Gradient.Vertical
            GradientStop { position: 0.0; color: Qt.lighter(core.stateColor, dormant ? 0.5 : 1.15) }
            GradientStop { position: 0.55; color: Theme.bg1 }
            GradientStop { position: 1.0; color: Theme.bg0 }
        }
        border.width: 1.5; border.color: core.stateColor
        opacity: dormant ? 0.45 : 1
        scale: 1.0
        SequentialAnimation on scale {
            running: core.ambientMotion
            loops: Animation.Infinite
            NumberAnimation { to: 1.03; duration: 2400; easing.type: Easing.InOutSine }
            NumberAnimation { to: 1.0; duration: 2400; easing.type: Easing.InOutSine }
        }
        Text {
            anchors.centerIn: parent
            text: "JARVIS"
            color: Theme.text
            font { family: Theme.font; pixelSize: core.width * 0.085; letterSpacing: 4; weight: Font.DemiBold }
        }
    }

    // UNKNOWN_OUTCOME speaks in its own language: an unstable two-tone pulse
    // that resembles neither success nor an ordinary error.
    Rectangle {
        anchors.centerIn: parent
        width: core.width * 0.62; height: width; radius: width / 2
        color: "transparent"
        border.width: 3; border.color: Theme.unknown
        visible: state_ === "unknown"
        opacity: 0
        SequentialAnimation on opacity {
            running: state_ === "unknown"
            loops: Animation.Infinite
            NumberAnimation { to: 0.9; duration: 380; easing.type: Easing.InOutCubic }
            NumberAnimation { to: 0.15; duration: 900; easing.type: Easing.InOutCubic }
            PauseAnimation { duration: 500 }
        }
    }

    // Completion flare: one restrained pulse, then back to rest.
    Rectangle {
        id: flare
        anchors.centerIn: parent
        width: core.width * 0.6; height: width; radius: width / 2
        color: "transparent"; border.width: 2; border.color: Theme.success
        opacity: 0; scale: 1
    }
    SequentialAnimation {
        id: flareAnim
        ParallelAnimation {
            NumberAnimation { target: flare; property: "opacity"; from: 0.9; to: 0; duration: 700; easing.type: Easing.OutCubic }
            NumberAnimation { target: flare; property: "scale"; from: 0.9; to: 1.7; duration: 700; easing.type: Easing.OutCubic }
        }
    }
    onState_Changed: if (state_ === "completed") flareAnim.restart()
}
