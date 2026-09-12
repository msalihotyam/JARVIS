import QtQuick
import QtQuick.Shapes
import QtQuick.Controls.Basic
import "../theme"

Item {
    id: power
    width: 42; height: 42
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
    Rectangle { anchors.centerIn: parent; width: power.hovered ? 57 : 51; height: width; radius: width / 2; color: "#ef334f"; opacity: power.engaged ? 0.18 : 0.08
        Behavior on width { NumberAnimation { duration: 180 } }
        SequentialAnimation on opacity { running: power.animate && Theme.ambient && power.visible && power.Window.active; loops: Animation.Infinite
            NumberAnimation { to: power.engaged ? 0.25 : 0.12; duration: 2600; easing.type: Easing.InOutSine }
            NumberAnimation { to: power.engaged ? 0.12 : 0.06; duration: 2600; easing.type: Easing.InOutSine }
        }
    }
    Rectangle { anchors.fill: parent; radius: width / 2; border.width: 1; border.color: power.hovered || power.engaged ? "#ff6b7e" : "#ad334d"
        gradient: Gradient { GradientStop { position: 0; color: "#471c2a" } GradientStop { position: 0.5; color: "#200b15" } GradientStop { position: 1; color: "#0d060b" } }
    }
    Rectangle { anchors.fill: parent; anchors.margins: 4; radius: width / 2; color: power.engaged ? "#40211320" : "transparent"; border.width: 1; border.color: "#60f7536b" }
    Shape { anchors.centerIn: parent; width: 20; height: 22; preferredRendererType: Shape.CurveRenderer
        ShapePath { strokeColor: "#ff6578"; strokeWidth: 1.8; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: 10; centerY: 12; radiusX: 7.5; radiusY: 7.5; startAngle: -50; sweepAngle: 280 }
        }
        ShapePath { strokeColor: "#ff8794"; strokeWidth: 1.8; fillColor: "transparent"; capStyle: ShapePath.RoundCap; startX: 10; startY: 2; PathLine { x: 10; y: 11 } }
    }
    Rectangle { anchors.fill: parent; anchors.margins: -4; radius: width / 2; color: "transparent"; border.width: 1; border.color: "#ffb1bb"; visible: power.activeFocus }
    MouseArea { id: hit; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: { power.forceActiveFocus(); power.clicked() } }
    ToolTip.visible: hovered
    ToolTip.delay: 500
    ToolTip.text: Accessible.description
}
