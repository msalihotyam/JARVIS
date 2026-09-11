import QtQuick
import "../theme"

// The environment behind everything. STATIC by design: a gradient horizon,
// a faint HUD grid and a fixed star field, plus one optional image asset
// slot for the canonical Earth/space plate. The only motion is the optional
// ambient shimmer of a few stars, and it pauses when the window is inactive.
Item {
    id: bg
    property bool active: true
    // The canonical background asset slot. Empty until a clean, UI-free
    // Earth/space plate is supplied; the neutral treatment below is the
    // development stand-in and is not the accepted canonical look.
    property url imageSource: ""
    readonly property bool hasImage: imageSource != ""

    Rectangle {
        anchors.fill: parent
        gradient: Gradient {
            GradientStop { position: 0.0; color: Theme.bg0 }
            GradientStop { position: 0.55; color: Theme.bg1 }
            GradientStop { position: 1.0; color: Theme.horizon }
        }
    }

    Image {
        anchors.fill: parent
        source: bg.imageSource
        visible: bg.hasImage
        fillMode: Image.PreserveAspectCrop
        asynchronous: true
        cache: true
        opacity: Theme.space ? 1.0 : 0.35
        Behavior on opacity { NumberAnimation { duration: Theme.themeMs; easing.type: Easing.InOutCubic } }
    }

    // A soft planetary limb: one large circle whose top edge sits on the
    // lower right, hinting at the orbital horizon of the reference.
    Rectangle {
        width: parent.width * 1.6; height: width; radius: width / 2
        x: parent.width * 0.05; y: parent.height * 0.82
        color: Theme.horizon
        opacity: bg.hasImage ? 0 : (Theme.space ? 0.9 : 0.35)
        border.width: 3
        border.color: Theme.hud
        Behavior on opacity { NumberAnimation { duration: Theme.themeMs } }
    }
    Rectangle {
        width: parent.width * 1.6; height: width; radius: width / 2
        x: parent.width * 0.05; y: parent.height * 0.82 + 10
        color: "transparent"
        border.width: 60
        border.color: Theme.glow
        opacity: (bg.hasImage ? 0.25 : 0.5) * Theme.glowStrength
    }

    // Faint HUD grid lines.
    Repeater {
        model: 6
        Rectangle {
            x: 0; y: (index + 1) * bg.height / 7
            width: bg.width; height: 1
            color: Theme.hud
            opacity: 0.22
        }
    }
    Repeater {
        model: 10
        Rectangle {
            x: (index + 1) * bg.width / 11; y: 0
            width: 1; height: bg.height
            color: Theme.hud
            opacity: 0.14
        }
    }

    // Star field: a deterministic pseudo-random scatter, painted once.
    Canvas {
        id: stars
        anchors.fill: parent
        opacity: Theme.starOpacity
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
        onPaint: {
            var ctx = getContext("2d")
            ctx.clearRect(0, 0, width, height)
            var seed = 7
            function rnd() { seed = (seed * 9301 + 49297) % 233280; return seed / 233280 }
            for (var i = 0; i < 260; i++) {
                var x = rnd() * width, y = rnd() * height * 0.8
                var r = 0.4 + rnd() * 1.3
                ctx.globalAlpha = 0.25 + rnd() * 0.6
                ctx.fillStyle = rnd() > 0.85 ? "#bfe6ff" : "#ffffff"
                ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill()
            }
        }
    }

    // Optional ambient shimmer: three stars breathe, slowly. Off = static.
    Repeater {
        model: 3
        Rectangle {
            property real px: [0.18, 0.62, 0.81][index]
            property real py: [0.22, 0.12, 0.34][index]
            x: bg.width * px; y: bg.height * py
            width: 3; height: 3; radius: 1.5
            color: "#dff3ff"
            opacity: 0.3
            SequentialAnimation on opacity {
                running: Theme.ambient && bg.active && Theme.space
                loops: Animation.Infinite
                NumberAnimation { to: 0.9; duration: 2600 + index * 900; easing.type: Easing.InOutSine }
                NumberAnimation { to: 0.3; duration: 2600 + index * 900; easing.type: Easing.InOutSine }
            }
        }
    }
}
