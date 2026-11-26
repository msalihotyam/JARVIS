import QtQuick

// FACEBAR1: the talk toggle. A click (never a hold) calls face.talkToggle("button"). Colour only while
// listening: accent border, accent_surface fill and a halo that widens with the newest face.levels value.
Item {
    id: talk
    readonly property var t: face.tokens
    readonly property real hit: t.type_control_line + 2 * t.space_12       // 44 px hit target
    readonly property bool still: t.motion_medium === 0
    readonly property string phase: typeof face.composer.state === "string" ? face.composer.state : ""
    readonly property bool listening: phase === "listening"
    readonly property real level: {
        var l = face.levels
        var v = l && typeof l === "object" && l.length > 0 ? Number(l[l.length - 1]) : 0
        return isFinite(v) ? Math.max(0, Math.min(1, v)) : 0
    }
    readonly property string label: typeof face.composer.talkLabel === "string" && face.composer.talkLabel !== ""
                                    ? face.composer.talkLabel : "Konuş"   // a broken model never leaves it nameless
    readonly property string clock: clockText(face.composer.elapsed)

    // m:ss (h:mm:ss past an hour) from whole seconds; "" for anything that is not a usable count
    function clockText(e) {
        if (typeof e !== "number" || !isFinite(e) || e < 0)
            return ""
        var d = new Date(0)
        d.setUTCSeconds(Math.floor(e))
        if (isNaN(d.getTime()))
            return ""
        var ss = ("0" + d.getUTCSeconds()).slice(-2)
        var h = d.getUTCHours()                                    // wraps after a day; not a real listen
        return h > 0 ? h + ":" + ("0" + d.getUTCMinutes()).slice(-2) + ":" + ss : d.getUTCMinutes() + ":" + ss
    }

    visible: face.talkAvailable === true
    enabled: phase !== "transcribing"
    implicitWidth: Math.max(hit, row.implicitWidth + 2 * t.space_14)
    implicitHeight: hit
    Accessible.role: Accessible.Button
    Accessible.name: label

    Rectangle {
        objectName: "talkHalo"
        readonly property real spread: talk.t.space_4 + (talk.still ? 0 : talk.level * talk.t.space_4)
        visible: talk.listening
        x: -spread
        y: -spread
        width: talk.width + 2 * spread
        height: talk.height + 2 * spread
        radius: talk.t.radius_control + spread
        color: talk.t.color_accent_line
    }
    Rectangle {
        objectName: "talkFace"
        anchors.fill: parent
        radius: talk.t.radius_control
        color: talk.listening ? talk.t.color_accent_surface : talk.t.color_surface2
        border.width: 1
        border.color: talk.listening ? talk.t.color_accent : talk.t.color_line2
    }
    Row {
        id: row
        anchors.centerIn: parent
        spacing: talk.t.space_8
        Rectangle {                                                // a dot, or the stop square while listening
            objectName: "talkGlyph"
            anchors.verticalCenter: parent.verticalCenter
            width: talk.t.space_10
            height: width
            radius: talk.listening ? 2 : width / 2
            color: talk.listening ? talk.t.color_accent : talk.enabled ? talk.t.color_text2 : talk.t.color_disabled_glyph
        }
        Text {
            anchors.verticalCenter: parent.verticalCenter
            width: Math.min(implicitWidth, talk.t.space_20 * 2 * 2)   // a runaway label elides, never widens the row
            elide: Text.ElideRight
            text: talk.label
            textFormat: Text.PlainText
            color: talk.enabled ? talk.t.color_text1 : talk.t.color_disabled_glyph
            font.pixelSize: talk.t.type_control_size
        }
        Text {
            objectName: "talkClock"
            anchors.verticalCenter: parent.verticalCenter
            visible: talk.listening && text !== ""
            text: talk.clock
            textFormat: Text.PlainText
            color: talk.t.color_text2
            font.family: "JetBrains Mono"
            font.pixelSize: talk.t.type_control_size
        }
    }
    MouseArea {
        anchors.fill: parent
        onClicked: face.talkToggle("button")
    }
}
