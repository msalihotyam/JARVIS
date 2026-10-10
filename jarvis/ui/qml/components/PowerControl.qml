import QtQuick
import QtQuick.Shapes
import "../theme"

Item {
    id: power
    // Authored at 42 px; the header may give it another size.
    property real diameter: 42
    // Gate 6G S2 skins (bridge.freezeKeySkin): 1 this classic key (default),
    // 2 the redesigned key, 3 a chrome toggle, 4 a knife switch in a torn
    // surface. Presentation only: the same click, signal and authority.
    property int skin: 1
    // The established execution state - never the click - places the lever:
    // running up (0), frozen down (1), anything on its way or unsure between.
    property string executionState: "running"
    readonly property real lever: executionState === "running" ? 0 : executionState === "frozen" ? 1 : 0.4
    // Size is one number per skin, its height; skins 3-4 take their art's width.
    readonly property real size: skin === 2 ? 44 : skin === 3 ? 84 : skin === 4 ? 112 : diameter
    width: art.item ? art.item.implicitWidth : size; height: size
    readonly property real k: diameter / 42
    signal clicked()
    property bool engaged: false
    readonly property bool hovered: hit.containsMouse
    activeFocusOnTab: true
    Accessible.role: Accessible.Button
    // Freeze / Resume JARVIS for the whole installation - not close, not quit, not cancel task.
    Accessible.name: engaged ? qsTr("Resume JARVIS") : qsTr("Freeze JARVIS")
    Accessible.description: engaged ? qsTr("JARVIS is frozen. Resuming asks for confirmation.") : qsTr("Freeze JARVIS: nothing new can start; running work pauses at its next safe point.")
    Accessible.onPressAction: if (enabled) clicked()
    Keys.onReturnPressed: if (enabled) clicked()
    Keys.onSpacePressed: if (enabled) clicked()
    opacity: enabled ? 1 : 0.5
    scale: hit.pressed ? 0.95 : 1
    Behavior on scale { NumberAnimation { duration: Theme.pressMs; easing.type: Theme.ease } }
    Item {
        objectName: "freezeKeyClassic"
        anchors.fill: parent
        visible: power.skin === 1
        // The halo is static (K2): it grows on hover and is stronger while frozen; it never breathes.
        Rectangle { anchors.centerIn: parent; width: (power.hovered ? 57 : 51) * power.k; height: width; radius: width / 2; color: "#ef334f"; opacity: power.engaged ? 0.18 : 0.08
            Behavior on width { NumberAnimation { duration: Theme.dur(180); easing.type: Theme.ease } }
        }
        Rectangle { anchors.fill: parent; radius: width / 2; border.width: 1; border.color: power.hovered || power.engaged ? "#ff7183" : "#c73a56"
            gradient: Gradient { GradientStop { position: 0; color: "#6b2032" } GradientStop { position: 0.48; color: "#2b0c17" } GradientStop { position: 1; color: "#12060c" } }
        }
        Rectangle { anchors.fill: parent; anchors.margins: 4 * power.k; radius: width / 2; color: power.engaged ? "#40211320" : "transparent"; border.width: 1; border.color: "#7dff5f76" }
        // The glyph says what the press does: pause bars to Freeze, a play
        // triangle to Resume. Never a power symbol - JARVIS is not switched off.
        Shape { anchors.centerIn: parent; width: 20; height: 22; scale: power.k; preferredRendererType: Shape.CurveRenderer
            ShapePath { strokeColor: power.engaged ? "transparent" : "#ffa3ad"; strokeWidth: 2.4; fillColor: "transparent"; capStyle: ShapePath.RoundCap
                startX: 7; startY: 5; PathLine { x: 7; y: 17 } PathMove { x: 13; y: 5 } PathLine { x: 13; y: 17 } }
            ShapePath { strokeColor: power.engaged ? "#ffa3ad" : "transparent"; strokeWidth: 2; fillColor: "transparent"; joinStyle: ShapePath.RoundJoin
                startX: 7; startY: 5; PathLine { x: 16.5; y: 11 } PathLine { x: 7; y: 17 } PathLine { x: 7; y: 5 } }
        }
    }
    FreezeKeyRedesign { anchors.fill: parent; visible: power.skin === 2; position: power.lever; hovered: power.hovered }
    Loader {
        id: art
        anchors.fill: parent
        active: power.skin === 3 || power.skin === 4
        sourceComponent: FreezeKeySwitch { kind: power.skin === 4 ? "knife" : "toggle"; position: power.lever }
    }
    Rectangle { anchors.fill: parent; anchors.margins: -4; radius: power.skin >= 3 ? 8 : width / 2; color: "transparent"; border.width: 1; border.color: "#ffb1bb"; visible: power.activeFocus }
    MouseArea { id: hit; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: { power.forceActiveFocus(); power.clicked() } }
    HoverTip { text: power.Accessible.name; offsetX: (width - tipWidth) / 2 }
}
