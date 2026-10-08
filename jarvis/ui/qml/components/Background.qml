import QtQuick
import QtQuick.Shapes
import "../theme"

// The environment behind everything. STATIC by design: a gradient sky, the
// Earth/space plate, a crown of light behind the core and a vignette. When
// no plate is loaded a drawn star field and planetary limb stand in. The
// only motion is the optional shimmer of three stars, and it pauses when
// inactive.
Item {
    id: bg
    property bool active: true
    // The plate. Two production skins: "main" (the chosen Canonical Space
    // plate) and "alternate". Each image carries its own drawn core, so the
    // plate is placed with that core exactly under the live JARVIS core
    // (`screenFocus`, set by the window) while still covering the window at
    // every size - never stretched, only scaled and shifted.
    property string skin: "main"
    property url overrideSource: ""                 // DEV audition only
    readonly property url imageSource: overrideSource != "" ? overrideSource
                                     : skin === "alternate" ? Qt.resolvedUrl("../assets/background-space-alt.webp")
                                     : Qt.resolvedUrl("../assets/background-space.webp")
    readonly property point imageFocus: overrideSource != "" ? Qt.point(0.4985, 0.087)
                                      : skin === "alternate" ? Qt.point(0.4991, 0.0914) : Qt.point(0.4979, 0.0824)
    property point screenFocus: Qt.point(0.5, 0.08)
    readonly property bool hasImage: plate.status === Image.Ready

    Rectangle {
        anchors.fill: parent
        gradient: Gradient {
            GradientStop { position: 0.0; color: Theme.bg0 }
            GradientStop { position: 0.55; color: Theme.bg1 }
            GradientStop { position: 1.0; color: Theme.horizon }
        }
    }

    Image {
        id: plate
        source: bg.imageSource
        visible: bg.hasImage
        asynchronous: true
        cache: true
        smooth: true
        readonly property real iw: implicitWidth > 0 ? implicitWidth : 1
        readonly property real ih: implicitHeight > 0 ? implicitHeight : 1
        // The smallest scale that both covers the window and lets the drawn
        // core land on the live one.
        readonly property real k: Math.max(bg.width / iw, bg.height / ih,
            bg.screenFocus.x * bg.width / (bg.imageFocus.x * iw),
            (1 - bg.screenFocus.x) * bg.width / ((1 - bg.imageFocus.x) * iw),
            bg.screenFocus.y * bg.height / (bg.imageFocus.y * ih),
            (1 - bg.screenFocus.y) * bg.height / ((1 - bg.imageFocus.y) * ih))
        width: Math.ceil(iw * k); height: Math.ceil(ih * k)
        x: Math.round(bg.screenFocus.x * bg.width - bg.imageFocus.x * width)
        y: Math.round(bg.screenFocus.y * bg.height - bg.imageFocus.y * height)
        opacity: Theme.space ? 1.0 : 0.35
        Behavior on opacity { NumberAnimation { duration: Theme.themeMs; easing.type: Easing.InOutCubic } }
    }

    // Star field: a deterministic scatter, painted once. Small and quiet.
    Canvas {
        id: stars
        anchors.fill: parent
        opacity: bg.hasImage ? 0 : Theme.starOpacity
        visible: opacity > 0
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
        onPaint: {
            var ctx = getContext("2d")
            ctx.clearRect(0, 0, width, height)
            var seed = 7
            function rnd() { seed = (seed * 9301 + 49297) % 233280; return seed / 233280 }
            for (var i = 0; i < 190; i++) {
                var x = rnd() * width, y = rnd() * height * 0.85
                var r = 0.3 + rnd() * 1.0
                ctx.globalAlpha = 0.18 + rnd() * 0.5
                ctx.fillStyle = rnd() > 0.85 ? "#bfe6ff" : "#ffffff"
                ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill()
            }
        }
    }

    // Atmospheric light low on the left: a wide, soft scatter with a faint
    // brighter horizon inside it. Lighting only - no drawn planet; the
    // supplied plate replaces it. Space theme only.
    Item {
        id: limb
        anchors.fill: parent
        opacity: (bg.hasImage || !Theme.space) ? 0 : 1
        Behavior on opacity { NumberAnimation { duration: Theme.themeMs } }
        readonly property real radius: bg.width * 1.5
        readonly property real cx: -bg.width * 0.5
        readonly property real cy: bg.height * 3.07
        Shape {
            id: haloShape
            x: limb.cx - limb.radius * 1.12; y: limb.cy - limb.radius * 1.12
            width: limb.radius * 2.24; height: width
            preferredRendererType: Shape.CurveRenderer
            opacity: Theme.glowStrength
            ShapePath {
                strokeWidth: -1
                fillGradient: RadialGradient {
                    centerX: haloShape.width / 2; centerY: haloShape.height / 2; centerRadius: haloShape.width / 2
                    focalX: centerX; focalY: centerY
                    GradientStop { position: 0.860; color: Theme.alpha(Theme.bg0, 0.0) }
                    GradientStop { position: 0.888; color: Theme.alpha(Theme.horizon, 0.55) }
                    GradientStop { position: 0.894; color: Theme.alpha(Theme.glow, 0.26) }
                    GradientStop { position: 0.915; color: Theme.alpha(Theme.glow, 0.10) }
                    GradientStop { position: 0.960; color: Theme.alpha(Theme.glow, 0.025) }
                    GradientStop { position: 1.000; color: "transparent" }
                }
                PathAngleArc { centerX: haloShape.width / 2; centerY: haloShape.height / 2; radiusX: haloShape.width / 2; radiusY: haloShape.height / 2; startAngle: 0; sweepAngle: 360 }
            }
        }
    }

    // Crown of light behind the core, top center.
    Shape {
        id: crown
        width: bg.width * 0.95; height: bg.height * 0.62
        x: (bg.width - width) / 2; y: -height * 0.52
        preferredRendererType: Shape.CurveRenderer
        opacity: Theme.glowStrength * (bg.hasImage ? 0.7 : 1)
        ShapePath {
            strokeWidth: -1
            fillGradient: RadialGradient {
                centerX: crown.width / 2; centerY: crown.height / 2; centerRadius: crown.width / 2
                focalX: centerX; focalY: centerY
                GradientStop { position: 0.00; color: Theme.alpha(Theme.glow, 0.34) }
                GradientStop { position: 0.30; color: Theme.alpha(Theme.glow, 0.12) }
                GradientStop { position: 0.62; color: Theme.alpha(Theme.glow, 0.03) }
                GradientStop { position: 1.00; color: "transparent" }
            }
            PathAngleArc { centerX: crown.width / 2; centerY: crown.height / 2; radiusX: crown.width / 2; radiusY: crown.height / 2; startAngle: 0; sweepAngle: 360 }
        }
        transform: Scale { origin.x: crown.width / 2; origin.y: crown.height / 2; yScale: 0.62 }
    }

    // Vignette: the edges fall away, so the lit center reads as foreground.
    Shape {
        id: vignette
        width: 1000; height: 1000
        preferredRendererType: Shape.CurveRenderer
        transform: Scale { xScale: bg.width * 1.5 / 1000; yScale: bg.height * 1.5 / 1000; origin.x: 0; origin.y: 0 }
        x: -bg.width * 0.25; y: -bg.height * 0.25
        ShapePath {
            strokeWidth: -1
            fillGradient: RadialGradient {
                centerX: 500; centerY: 500; centerRadius: 500; focalX: 500; focalY: 500
                GradientStop { position: 0.00; color: "transparent" }
                GradientStop { position: 0.42; color: "transparent" }
                GradientStop { position: 0.72; color: Qt.rgba(0, 0, 0, 0.28) }
                GradientStop { position: 1.00; color: Qt.rgba(0, 0, 0, 0.62) }
            }
            PathAngleArc { centerX: 500; centerY: 500; radiusX: 500; radiusY: 500; startAngle: 0; sweepAngle: 360 }
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
                NumberAnimation { to: 0.9; duration: Theme.dur(2600 + index * 900); easing.type: Easing.InOutSine }
                NumberAnimation { to: 0.3; duration: Theme.dur(2600 + index * 900); easing.type: Easing.InOutSine }
            }
        }
    }
}
