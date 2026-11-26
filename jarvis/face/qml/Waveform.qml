import QtQuick

// FACEBAR1: the listening waveform. Bars come from face.levels (0-1, newest last); a flat line when the
// list is empty or motion is reduced. Values outside 0-1 or not numbers are drawn clamped, never trusted.
Item {
    id: wave
    readonly property var t: face.tokens
    readonly property bool still: t.motion_medium === 0
    readonly property real bar: t.space_6 / 2                     // 3 px bars with a 3 px gap
    readonly property var shown: {
        var src = face.levels, out = []
        if (still || !src || typeof src !== "object" || !(src.length > 0))
            return out
        var fit = Math.max(0, Math.floor((width + bar) / (bar * 2)))
        for (var i = Math.max(0, src.length - fit); i < src.length; i++) {
            var v = Number(src[i])
            out.push(isFinite(v) ? Math.max(0, Math.min(1, v)) : 0)
        }
        return out
    }
    implicitHeight: t.type_turn_line

    Rectangle {
        objectName: "waveFlat"
        visible: wave.shown.length === 0
        anchors.verticalCenter: parent.verticalCenter
        width: parent.width
        height: 2
        radius: 1
        color: wave.t.color_line2
    }
    Row {
        objectName: "waveBars"
        anchors.verticalCenter: parent.verticalCenter
        spacing: wave.bar
        Repeater {
            model: wave.shown
            Rectangle {
                required property var modelData
                anchors.verticalCenter: parent.verticalCenter
                width: wave.bar
                height: Math.max(2, modelData * wave.height)
                radius: wave.bar / 2
                color: wave.t.color_text2
            }
        }
    }
}
