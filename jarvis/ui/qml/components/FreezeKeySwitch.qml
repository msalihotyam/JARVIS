import QtQuick
import "../theme"

// Freeze key skins 3 (chrome toggle) and 4 (knife switch in a torn surface),
// Gate 6G S2. Three layers cut by dev/assets/freeze_keys.py: the plate, and
// the lever up (running) and down (frozen). The lever turns over toward the
// viewer about its hinge line, drawn as a vertical scale through zero.
// `position` (0 up, 1 down, between = on its way) comes from the established
// execution state only, never from the click. The toggle snaps (~80 ms and a
// tiny settle), the knife swings slower and weighted; ambient off = no motion.
Item {
    id: sw
    property string kind: "toggle"
    property real position: 0
    // The drawn lever; it follows `position`.
    property real shown: 0
    readonly property bool knife: kind === "knife"
    readonly property real pivot: knife ? 0.5424 : 0.4583      // printed by freeze_keys.py
    // +1 the lever stands up, 0 it points at the viewer, -1 it hangs down.
    readonly property real lift: 1 - 2 * shown
    objectName: "freezeKeySwitch"
    implicitWidth: height * (knife ? 345 / 442 : 215 / 360)

    Component.onCompleted: shown = position
    onPositionChanged: {
        swing.stop()
        if (!Theme.ambient) { shown = position; return }
        // A lever that lands at an end goes a hair past it and settles back.
        const end = position === 0 || position === 1
        flip.to = end ? position + (position - shown) * (knife ? 0.03 : 0.05) : position
        settle.to = position
        swing.start()
    }
    SequentialAnimation {
        id: swing
        NumberAnimation { id: flip; target: sw; property: "shown"; duration: Theme.dur(sw.knife ? 420 : 80); easing.type: sw.knife ? Easing.InOutCubic : Easing.InQuad }
        NumberAnimation { id: settle; target: sw; property: "shown"; duration: Theme.dur(sw.knife ? 140 : 50); easing.type: Easing.OutCubic }
    }

    Image { anchors.fill: parent; source: "../assets/freeze-" + sw.kind + "-plate.png"; mipmap: true; smooth: true }
    Image {
        objectName: "freezeLeverUp"
        anchors.fill: parent; source: "../assets/freeze-" + sw.kind + "-up.png"; mipmap: true; smooth: true
        visible: sw.lift > 0.01
        transform: Scale { origin.y: sw.height * sw.pivot; yScale: Math.max(0, sw.lift) }
    }
    Image {
        objectName: "freezeLeverDown"
        anchors.fill: parent; source: "../assets/freeze-" + sw.kind + "-down.png"; mipmap: true; smooth: true
        visible: sw.lift < -0.01
        transform: Scale { origin.y: sw.height * sw.pivot; yScale: Math.max(0, -sw.lift) }
    }
}
