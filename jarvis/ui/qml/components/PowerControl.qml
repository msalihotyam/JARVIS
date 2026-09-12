import QtQuick
import QtQuick.Shapes
import QtQuick.Controls.Basic
import "../theme"

Item {
    id: power
    // Authored at 42 px; the header may give it another size.
    property real diameter: 42
    width: diameter; height: diameter
    readonly property real k: diameter / 42
    signal clicked()
    property bool engaged: false
    property bool animate: true
    readonly property bool hovered: hit.containsMouse
    activeFocusOnTab: true
    Accessible.role: Accessible.Button
    Accessible.name: "Kill Switch"
    Accessible.description: engaged ? "Re-enable JARVIS execution" : "Disable JARVIS execution"
    Accessible.onPressAction: if (enabled) clicked()
    Keys.onReturnPressed: if (enabled) clicked()
    Keys.onSpacePressed: if (enabled) clicked()
    opacity: enabled ? 1 : 0.5
    scale: hit.pressed ? 0.95 : 1
    Behavior on scale { NumberAnimation { duration: 100 } }
    Rectangle { anchors.centerIn: parent; width: (power.hovered ? 57 : 51) * power.k; height: width; radius: width / 2; color: "#ef334f"; opacity: power.engaged ? 0.18 : 0.08
        Behavior on width { NumberAnimation { duration: 180 } }
        SequentialAnimation on opacity { running: power.animate && Theme.ambient && power.visible && power.Window.active; loops: Animation.Infinite
            NumberAnimation { to: power.engaged ? 0.25 : 0.12; duration: 2600; easing.type: Easing.InOutSine }
            NumberAnimation { to: power.engaged ? 0.12 : 0.06; duration: 2600; easing.type: Easing.InOutSine }
        }
    }
    Rectangle { anchors.fill: parent; radius: width / 2; border.width: 1; border.color: power.hovered || power.engaged ? "#ff7183" : "#c73a56"
        gradient: Gradient { GradientStop { position: 0; color: "#6b2032" } GradientStop { position: 0.48; color: "#2b0c17" } GradientStop { position: 1; color: "#12060c" } }
    }
    Rectangle { anchors.fill: parent; anchors.margins: 4 * power.k; radius: width / 2; color: power.engaged ? "#40211320" : "transparent"; border.width: 1; border.color: "#7dff5f76" }
    Shape { anchors.centerIn: parent; width: 20; height: 22; scale: power.k; preferredRendererType: Shape.CurveRenderer
        ShapePath { strokeColor: "#ff8593"; strokeWidth: 2; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: 10; centerY: 12; radiusX: 7.5; radiusY: 7.5; startAngle: -50; sweepAngle: 280 }
        }
        ShapePath { strokeColor: "#ffa3ad"; strokeWidth: 2; fillColor: "transparent"; capStyle: ShapePath.RoundCap; startX: 10; startY: 2; PathLine { x: 10; y: 11 } }
    }
    Rectangle { anchors.fill: parent; anchors.margins: -4; radius: width / 2; color: "transparent"; border.width: 1; border.color: "#ffb1bb"; visible: power.activeFocus }
    MouseArea { id: hit; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: { power.forceActiveFocus(); power.clicked() } }
    ToolTip.visible: hovered
    ToolTip.delay: 500
    ToolTip.text: Accessible.description
}
