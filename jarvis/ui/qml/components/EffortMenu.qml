import QtQuick
import "../theme"

// Gate 6G M3 (G6CM1): the "Effort ⌄" chip under the composer and its mini
// popover - title "Effort <level>" and a Faster <-> Smarter track with ONE stop
// per level the selected model supports (what its CLI reported). The handle is
// dragged and snaps to the stops; a press on a stop or the track jumps there;
// the title follows live. The level is written once, on release, through the
// owner's own call (`picked`). "Recommended" sits under the provider's default
// level when it reported one. No level is invented; none reported, no chip.
Item {
    id: effort
    objectName: "effortMenuChip"
    property var levels: []
    property string value: ""
    property string recommended: ""
    signal picked(string level)

    // Q8: low / medium / high read in the window's language; others as they are.
    function label(e) { return e === "low" ? qsTr("low") : e === "medium" ? qsTr("medium") : e === "high" ? qsTr("high") : e }
    // The stop the handle stands on: the chosen level; with none chosen the
    // provider's default, which is what the CLI then uses.
    readonly property int index: levels.indexOf(value) >= 0 ? levels.indexOf(value) : levels.indexOf(recommended)
    readonly property int shown: track.dragging ? track.dragIndex : index
    readonly property string shownLabel: shown >= 0 && (track.dragging || value) ? label(levels[shown]) : qsTr("default")

    visible: levels.length > 0
    implicitWidth: face.width; implicitHeight: 30
    width: visible ? implicitWidth : 0; height: implicitHeight
    enabled: !bridge.configRefused
    opacity: enabled ? 1 : 0.5
    Accessible.role: Accessible.Button
    Accessible.name: qsTr("Effort %1").arg(shownLabel)

    Rectangle {
        id: face
        width: faceRow.width + 20; height: 30; radius: Theme.radiusSmall
        color: hit.containsMouse || pop.visible ? Theme.alpha(Theme.text, 0.08) : "transparent"
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
        Row { id: faceRow; anchors.centerIn: parent; spacing: 6
            Text { objectName: "effortMenuChipLabel"; anchors.verticalCenter: parent.verticalCenter
                   text: effort.value ? effort.label(effort.value) : qsTr("Effort")
                   color: Theme.textMuted; renderType: Theme.textRender; font: Theme.composerChip }
            Icon { anchors.verticalCenter: parent.verticalCenter; name: "chevron-down"; size: 12; tint: Theme.textMuted } }
        MouseArea { id: hit; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                    onClicked: pop.visible ? pop.close() : pop.open() }
    }

    MenuSurface {
        id: pop
        objectName: "effortMenu"
        trigger: effort; takesFocus: false
        restY: -height - 8; x: effort.width - width
        width: 300; padding: 14
        Column {
            width: pop.availableWidth
            spacing: 10
            Row { spacing: 8
                Text { anchors.baseline: level.baseline; text: qsTr("Effort"); color: Theme.textMuted; renderType: Theme.textRender; font: Theme.caption }
                Text { id: level; objectName: "effortMenuTitle"; text: effort.shownLabel; color: Theme.text; renderType: Theme.textRender; font: Theme.captionStrong } }
            Item { width: parent.width; height: faster.height
                Text { id: faster; text: qsTr("Faster"); color: Theme.textMuted; renderType: Theme.textRender; font: Theme.caption }
                Text { anchors.right: parent.right; text: qsTr("Smarter"); color: Theme.textMuted; renderType: Theme.textRender; font: Theme.caption } }
            Rectangle {
                id: track
                objectName: "effortTrack"
                width: parent.width; height: 34; radius: 9
                color: Theme.alpha(Theme.text, 0.08)
                border.width: 1; border.color: activeFocus ? Theme.glassEdgeStrong : Theme.alpha(Theme.accent, 0.25)
                readonly property int count: effort.levels.length
                readonly property real slot: count > 0 ? width / count : width
                function centre(i) { return (i + 0.5) * slot }
                function nearest(x) { return Math.max(0, Math.min(count - 1, Math.floor(x / slot))) }
                property bool dragging: false
                property int dragIndex: -1
                function commit(i) { if (i >= 0 && effort.levels[i] !== effort.value) effort.picked(effort.levels[i]) }
                activeFocusOnTab: true
                Accessible.role: Accessible.Slider
                Accessible.name: qsTr("Effort")
                Accessible.description: effort.shownLabel
                Keys.onLeftPressed: commit(Math.max(0, effort.index - 1))
                Keys.onRightPressed: commit(Math.min(count - 1, effort.index + 1))
                Repeater {
                    model: effort.levels
                    Rectangle { required property int index; width: 5; height: 5; radius: 2.5
                                x: track.centre(index) - width / 2; anchors.verticalCenter: parent.verticalCenter
                                color: Theme.alpha(Theme.text, 0.45) }
                }
                Rectangle {
                    objectName: "effortHandle"
                    visible: effort.shown >= 0
                    width: 26; height: 24; radius: 8
                    x: track.centre(Math.max(0, effort.shown)) - width / 2; anchors.verticalCenter: parent.verticalCenter
                    Behavior on x { NumberAnimation { duration: Theme.dur(120); easing.type: Theme.ease } }
                    color: Theme.text
                }
                MouseArea {
                    id: drag
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    preventStealing: true
                    onPressed: function(mouse) { track.dragIndex = track.nearest(mouse.x); track.dragging = true }
                    onPositionChanged: function(mouse) { if (pressed) track.dragIndex = track.nearest(mouse.x) }
                    onReleased: { track.dragging = false; track.commit(track.dragIndex) }
                    onCanceled: track.dragging = false
                }
            }
            Item { width: parent.width; height: rec.height
                Text { id: rec; objectName: "effortRecommended"
                       readonly property int at: effort.levels.indexOf(effort.recommended)
                       visible: at >= 0
                       x: Math.max(0, Math.min(parent.width - width, track.centre(Math.max(0, at)) - width / 2))
                       text: qsTr("Recommended"); color: Theme.textMuted; renderType: Theme.textRender; font: Theme.caption } }
        }
    }
}
