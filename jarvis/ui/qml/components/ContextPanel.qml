import QtQuick
import QtQuick.Effects
import "../theme"

// Anchored context surface; the triggers remain above the outside-click
// layer. Geometry is always recomputed from the trigger and viewport.
Item {
    id: context
    anchors.fill: parent
    property bool open: false
    property point anchorPoint: Qt.point(24, 88)
    property real panelWidth: 430
    property real panelHeight: 540
    property string heading: ""
    property string surfaceName: "contextSurface"
    property Component header: null     // replaces the caption when set
    property color surfaceEdge: "transparent"   // a stronger edge, when set
    property color surfaceFill: "transparent"   // a deeper body, when set
    property bool surfaceBrackets: true
    // An optional decorative nine-slice frame of light (glow + rims) drawn
    // around the surface, and the surface radius it was drawn for. Every
    // other context surface leaves both unset and keeps its theme border.
    property url surfaceShell: ""
    property int surfaceShellMargin: 0
    property int surfaceShellBorder: 0
    property int surfaceRadius: 0
    default property alias content: inner.data
    readonly property real surfaceX: panel.x
    readonly property real surfaceY: panel.y
    readonly property real surfaceWidth: panel.width
    readonly property real surfaceHeight: panel.height
    visible: open || panel.opacity > 0
    enabled: open
    z: 400
    onOpenChanged: if (open) panel.forceActiveFocus()
    Rectangle { anchors.fill: parent; color: "#000000"; opacity: context.open ? 0.18 : 0
        Behavior on opacity { NumberAnimation { duration: Theme.panelIn } }
        MouseArea { anchors.fill: parent; onClicked: context.open = false }
    }
    GlassPanel {
        id: panel
        objectName: context.surfaceName
        raised: true; brackets: context.surfaceBrackets; padding: 22
        radius: context.surfaceRadius > 0 ? context.surfaceRadius : Theme.radius
        border.color: context.surfaceShell != "" ? "transparent" : context.surfaceEdge.a > 0 ? context.surfaceEdge : Theme.border
        color: context.surfaceFill.a > 0 ? context.surfaceFill : Theme.glassMenu
        // A luminous halo around a strong edge.
        // Above the body fill (a negative z would sit behind it); its centre is
        // transparent and the surface padding keeps it clear of the content.
        BorderImage { parent: panel; z: 0.5; visible: context.surfaceShell != ""; source: context.surfaceShell
            layer.enabled: context.surfaceShell != ""
            layer.effect: MultiEffect { colorization: 0.85; colorizationColor: "#55b7ff"; brightness: 0.12 }
            anchors.fill: parent; anchors.margins: -context.surfaceShellMargin
            border { left: context.surfaceShellBorder; top: context.surfaceShellBorder; right: context.surfaceShellBorder; bottom: context.surfaceShellBorder } }
        Rectangle { parent: panel; z: -1; visible: context.surfaceEdge.a > 0 && context.surfaceShell == ""; anchors.fill: parent; anchors.margins: -3; radius: panel.radius + 3
            color: "transparent"; border.width: 3; border.color: Qt.rgba(context.surfaceEdge.r, context.surfaceEdge.g, context.surfaceEdge.b, 0.22) }
        width: Math.min(context.panelWidth, context.width - 32)
        height: Math.min(context.panelHeight, context.height - 108)
        x: Math.max(16, Math.min(context.anchorPoint.x, context.width - width - 16))
        y: Math.max(80, Math.min(context.anchorPoint.y, context.height - height - 16))
        opacity: context.open ? 1 : 0
        // Opening ~220 ms, closing slightly faster; no bounce.
        transform: Translate { y: context.open ? 0 : -7; Behavior on y { NumberAnimation { duration: context.open ? Theme.panelIn : Theme.panelOut; easing.type: Easing.OutCubic } } }
        Behavior on opacity { NumberAnimation { duration: context.open ? Theme.panelIn : Theme.panelOut; easing.type: Easing.OutCubic } }
        Keys.onEscapePressed: context.open = false
        MouseArea { parent: panel; anchors.fill: parent; z: -0.5 }
        Item { id: heading; width: parent.width; height: context.header ? 50 : 34
            Text { visible: !context.header; anchors.verticalCenter: parent.verticalCenter; text: context.heading; color: Theme.text; font { family: Theme.font; pixelSize: 13; letterSpacing: 2.4; weight: Font.DemiBold } }
            Loader { active: !!context.header; sourceComponent: context.header; anchors.verticalCenter: parent.verticalCenter; width: parent.width - 44 }
            IconButton { objectName: context.surfaceName + "Close"; anchors.right: parent.right; glyph: "✕"; tip: qsTr("Close"); onClicked: context.open = false }
        }
        Rectangle { visible: !context.header; anchors.top: heading.bottom; width: parent.width; height: 1; color: Theme.border }
        Item { id: inner; anchors { left: parent.left; right: parent.right; top: heading.bottom; bottom: parent.bottom; topMargin: context.header ? 16 : 20 } }
    }
}
