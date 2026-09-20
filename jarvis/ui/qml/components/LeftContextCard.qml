import QtQuick
import QtQuick.Shapes
import QtQuick.Effects
import "../theme"

// The same blue glass as the accepted summary rail. Weather is the operator's
// fixed place as the bridge projects it (fresh, stale or unavailable); QML only
// shows it and never fetches anything. Without a reading the card claims no city.
Rectangle {
    id: context
    objectName: "leftContextCard"
    property date now: new Date()
    readonly property bool compact: width < 190
    readonly property bool tight: width < 155
    readonly property int inset: tight ? 9 : 13
    readonly property real weatherWidth: tight ? 49 : compact ? 57 : 68
    // Formatted in the interface language's locale; the time itself is never reinterpreted.
    readonly property string timeText: now.toLocaleTimeString(Qt.locale(i18n.qtLocale), "HH:mm")
    readonly property string dateText: now.toLocaleDateString(Qt.locale(i18n.qtLocale), tight ? "d MMM yyyy" : "ddd, d MMM")
    readonly property string zoneText: {
        var minutes = -now.getTimezoneOffset()
        var sign = minutes < 0 ? "-" : "+"
        minutes = Math.abs(minutes)
        return "UTC" + sign + ("0" + Math.floor(minutes / 60)).slice(-2) + ":" + ("0" + minutes % 60).slice(-2)
    }
    readonly property var wx: bridge.weather
    readonly property bool hasWeather: wx.available === true
    readonly property bool stale: hasWeather && wx.state !== "fresh"
    readonly property string temperatureText: hasWeather ? wx.temperature + "°" : ""
    readonly property string readingText: hasWeather
        ? new Date(wx.observed_at * 1000).toLocaleTimeString(Qt.locale(i18n.qtLocale), "HH:mm") : ""
    // Tomorrow.io's documented codes, mapped in Python to these keys; an unknown code shows no condition.
    function conditionText(key) {
        switch (key) {
        case "clear": return qsTr("Clear", "weather")
        case "mostly_clear": return qsTr("Mostly clear")
        case "partly_cloudy": return qsTr("Partly cloudy")
        case "mostly_cloudy": return qsTr("Mostly cloudy")
        case "cloudy": return qsTr("Cloudy")
        case "fog": return qsTr("Fog")
        case "light_fog": return qsTr("Light fog")
        case "drizzle": return qsTr("Drizzle")
        case "rain": return qsTr("Rain")
        case "light_rain": return qsTr("Light rain")
        case "heavy_rain": return qsTr("Heavy rain")
        case "snow": return qsTr("Snow")
        case "flurries": return qsTr("Flurries")
        case "light_snow": return qsTr("Light snow")
        case "heavy_snow": return qsTr("Heavy snow")
        case "freezing_drizzle": return qsTr("Freezing drizzle")
        case "freezing_rain": return qsTr("Freezing rain")
        case "light_freezing_rain": return qsTr("Light freezing rain")
        case "heavy_freezing_rain": return qsTr("Heavy freezing rain")
        case "ice_pellets": return qsTr("Ice pellets")
        case "heavy_ice_pellets": return qsTr("Heavy ice pellets")
        case "light_ice_pellets": return qsTr("Light ice pellets")
        case "thunderstorm": return qsTr("Thunderstorm")
        }
        return ""
    }
    readonly property string conditionLabel: hasWeather ? conditionText(wx.condition) : ""
    // The instrument's mark, by family: sun, cloud/fog bars, or falling lines.
    readonly property string family: {
        var c = hasWeather ? String(wx.condition) : ""
        if (c === "clear" || c === "mostly_clear") return "sun"
        if (c.indexOf("cloud") >= 0 || c.indexOf("fog") >= 0) return "cloud"
        return c ? "fall" : ""
    }
    radius: 14
    color: Theme.glassSecondaryShell
    Accessible.role: Accessible.Pane
    Accessible.name: hasWeather
        ? qsTr("%1, %2. %3, %4 %5, reading at %6.").arg(timeText).arg(dateText).arg(wx.district)
              .arg(temperatureText).arg(conditionLabel).arg(readingText)
        : qsTr("%1, %2, Local time, %3. Weather unavailable.").arg(timeText).arg(dateText).arg(zoneText)

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
        // The role size, capped to the room the column has. Until the column
        // and the metrics have a real width (the first bindings run before
        // layout, when the column is 0 or negative) the role size stands, so
        // the size is never 0 or negative.
        readonly property int clockPx: {
            var role = Theme.clockSize(tier)
            if (clockRoom <= 0 || clockMetrics.advanceWidth <= 0) return role
            return Math.max(1, Math.min(role, Math.floor(role * clockRoom / clockMetrics.advanceWidth)))
        }
        Text {
            objectName: "contextClock"
            width: timeGroup.clockRoom
            text: context.timeText; color: Theme.text
            font: Theme.clock(timeGroup.clockPx)
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
            Text { objectName: "contextPlace"; renderType: Theme.textRender; width: parent.width; elide: Text.ElideRight
                   text: context.hasWeather ? context.wx.district : context.compact ? qsTr("Local") : qsTr("Local time")
                   color: Theme.text; font: context.compact ? Theme.caption : Theme.body }
            // With a reading: when the provider says it applies ("last reading" once it is no longer current).
            Text { objectName: "contextTimeZone"; renderType: Theme.textRender; width: parent.width; elide: Text.ElideRight
                   text: !context.hasWeather ? context.zoneText
                         : context.stale ? qsTr("Last reading %1").arg(context.readingText) : qsTr("As of %1").arg(context.readingText)
                   color: Theme.textLabel; font: Theme.caption }
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
            id: mark
            objectName: "contextWeatherMark"
            property string family: context.family
            opacity: context.stale ? 0.55 : 1
            Rectangle { anchors.fill: parent; radius: width / 2; color: "#2036aaff"; border.color: "#596d9ebe" }
            Rectangle { anchors.fill: parent; anchors.margins: 5; radius: width / 2; color: "transparent"; border.color: "#7197b2"; opacity: 0.7 }
            // Unavailable: the neutral slashed instrument, never a fabricated sun.
            Rectangle { visible: mark.family === ""; anchors.centerIn: parent; width: parent.width * 0.7; height: 1.8; rotation: -45; color: "#c2dff4" }
            Rectangle { visible: mark.family === "sun"; anchors.centerIn: parent; width: parent.width * 0.34; height: width; radius: width / 2; color: "#d8ecfa" }
            Column {
                visible: mark.family === "cloud"; anchors.centerIn: parent; spacing: mark.width * 0.09
                Repeater { model: [0.5, 0.36, 0.5]
                    Rectangle { anchors.horizontalCenter: parent.horizontalCenter; width: mark.width * modelData; height: 1.8; radius: 1; color: "#c2dff4" } }
            }
            Row {
                visible: mark.family === "fall"; anchors.centerIn: parent; spacing: mark.width * 0.1
                Repeater { model: 3
                    Rectangle { width: 1.8; height: mark.width * 0.3; radius: 1; rotation: 18; color: "#c2dff4" } }
            }
        }
        Text {
            objectName: "contextTemperature"
            width: parent.width; horizontalAlignment: Text.AlignHCenter
            visible: context.hasWeather
            text: context.temperatureText; color: context.stale ? Theme.textMuted : Theme.text
            font { family: Theme.display; pixelSize: context.compact ? 25 : 31; weight: Font.Medium }
        }
        Text {
            objectName: "contextWeatherCondition"
            renderType: Theme.textRender
            width: parent.width; horizontalAlignment: Text.AlignHCenter
            // Unavailable: said whole - the narrowest rail takes the shorter phrase rather than an ellipsis.
            text: context.hasWeather ? context.conditionLabel : context.tight ? qsTr("No\nweather") : qsTr("Weather\nunavailable")
            wrapMode: Text.WordWrap; maximumLineCount: context.hasWeather ? 2 : 3; elide: Text.ElideRight
            color: context.stale ? Theme.textMuted : Theme.textLabel; font: Theme.caption
        }
    }
}
