import QtQuick
import QtQuick.Shapes
import QtQuick.Effects
import "../theme"

// The same blue glass as the accepted summary rail. The product has no
// weather or location provider: a time zone is context, not a city claim.
Rectangle {
    id: context
    objectName: "leftContextCard"
    property date now: new Date()
    readonly property bool compact: width < 190
    readonly property bool tight: width < 155
    readonly property int inset: tight ? 9 : 13
    readonly property real weatherWidth: tight ? 49 : compact ? 57 : 68
    readonly property string timeText: Qt.formatTime(now, "HH:mm")
    readonly property string dateText: Qt.formatDate(now, tight ? "d MMM yyyy" : "ddd, d MMM")
    readonly property string zoneText: {
        var minutes = -now.getTimezoneOffset()
        var sign = minutes < 0 ? "-" : "+"
        minutes = Math.abs(minutes)
        return "UTC" + sign + ("0" + Math.floor(minutes / 60)).slice(-2) + ":" + ("0" + minutes % 60).slice(-2)
    }
    radius: 14
    color: Theme.glassSecondaryShell
    Accessible.role: Accessible.Pane
    Accessible.name: timeText + ", " + dateText + ", Local time, " + zoneText + ". Weather not connected."

    BorderImage {
        anchors.fill: parent; anchors.margins: -12
        source: "../assets/rail-card-shell.png"
        border { left: 34; top: 34; right: 34; bottom: 34 }
        layer.enabled: true
        layer.effect: MultiEffect { colorization: 0.85; colorizationColor: "#55b7ff"; brightness: 0.12 }
    }
    Rectangle {
        anchors.fill: parent; anchors.margins: 1; radius: 13
        gradient: Gradient {
            GradientStop { position: 0; color: Theme.glassMicro }
            GradientStop { position: 0.65; color: "transparent" }
        }
    }
    Column {
        id: timeGroup
        x: context.inset; y: context.compact ? 10 : 12
        width: parent.width - x - context.weatherWidth - context.inset
        spacing: context.compact ? 2 : 4
        // Clock: Space Grotesk Medium with tabular figures, at the role size for
        // the viewport - capped to the clock column the card really has, so a
        // narrow rail never pushes the time into the weather instrument.
        readonly property int tier: Theme.tier(context.Window.width)
        TextMetrics { id: clockMetrics; text: "00:00"; font: Theme.clock(Theme.clockSize(timeGroup.tier)) }
        // The room the clock really has: its own column, which ends before the
        // weather column (the card's accepted rule - the time never crosses it).
        readonly property real clockRoom: width
        Text {
            objectName: "contextClock"
            width: timeGroup.clockRoom
            text: context.timeText; color: Theme.text
            font: Theme.clock(Math.min(Theme.clockSize(timeGroup.tier), Math.floor(Theme.clockSize(timeGroup.tier) * width / Math.max(1, clockMetrics.advanceWidth))))
            lineHeightMode: Text.FixedHeight; lineHeight: Theme.clockLine(timeGroup.tier)
        }
        Text {
            objectName: "contextDate"
            width: parent.width; text: context.dateText; color: Theme.textLabel; elide: Text.ElideRight
            font: Theme.dateText; lineHeightMode: Text.FixedHeight; lineHeight: 18
        }
    }
    Item {
        id: local
        objectName: "contextLocation"
        x: context.inset; y: parent.height - height - (context.compact ? 12 : 14)
        width: timeGroup.width; height: 35
        Shape {
            id: pin
            visible: !context.tight
            width: 15; height: 20; y: 1
            preferredRendererType: Shape.CurveRenderer
            ShapePath {
                strokeColor: Theme.materialBlue; strokeWidth: 1.7; fillColor: "transparent"
                startX: 7.5; startY: 19
                PathCubic { x: 1; y: 7; control1X: 5; control1Y: 15; control2X: 1; control2Y: 11 }
                PathCubic { x: 14; y: 7; control1X: 1; control1Y: -1; control2X: 14; control2Y: -1 }
                PathCubic { x: 7.5; y: 19; control1X: 14; control1Y: 11; control2X: 10; control2Y: 15 }
            }
            Rectangle { x: 5; y: 4; width: 5; height: 5; radius: 2.5; color: "transparent"; border.color: Theme.materialGlint }
        }
        Column {
            x: pin.visible ? 21 : 0; width: parent.width - x; spacing: 2
            Text { text: context.compact ? "Local" : "Local time"; color: Theme.text; font { family: Theme.font; pixelSize: context.compact ? 12 : 14 } }
            Text { objectName: "contextTimeZone"; text: context.zoneText; color: Theme.textLabel; font { family: Theme.font; pixelSize: context.tight ? 10 : 11 } }
        }
    }
    Column {
        id: weather
        objectName: "contextWeather"
        x: parent.width - width - context.inset
        y: 12
        width: context.weatherWidth
        spacing: context.compact ? 3 : 5
        Item {
            anchors.horizontalCenter: parent.horizontalCenter
            width: context.compact ? 29 : 38; height: width
            // A neutral unavailable instrument, not a fabricated sun/moon.
            Rectangle { anchors.fill: parent; radius: width / 2; color: "#2036aaff"; border.color: "#596d9ebe" }
            Rectangle { anchors.fill: parent; anchors.margins: 5; radius: width / 2; color: "transparent"; border.color: "#7197b2"; opacity: 0.7 }
            Rectangle { anchors.centerIn: parent; width: parent.width * 0.7; height: 1.8; rotation: -45; color: "#c2dff4" }
        }
        Text {
            objectName: "contextTemperature"
            width: parent.width; horizontalAlignment: Text.AlignHCenter
            text: "--°"; color: Theme.text
            font { family: Theme.display; pixelSize: context.compact ? 25 : 31; weight: Font.Medium }
        }
        Text {
            objectName: "contextWeatherCondition"
            width: parent.width; horizontalAlignment: Text.AlignHCenter
            text: "Not\nconnected"; wrapMode: Text.WordWrap
            Accessible.name: "Weather not connected"
            color: "#acc8df"; font { family: Theme.font; pixelSize: context.tight ? 10 : context.compact ? 11 : 12 }
        }
    }
}
