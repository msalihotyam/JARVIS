import QtQuick
import "../theme"

// The sidebar's clock strip (Gate 6F, L2 g): one thin line at its foot - the
// time, the date, then the weather (or the time zone without a reading).
// Weather is the operator's fixed place as the bridge projects it (fresh,
// stale or unavailable); QML only shows it and never fetches anything.
// Without a reading the strip claims no city. The right part gives way
// piece by piece when the line is short; the whole sentence is the strip's
// tip and its accessible name, so nothing is lost.
Rectangle {
    id: context
    objectName: "leftContextCard"
    property date now: new Date()
    property bool big: true
    // Formatted in the interface language's locale; the time itself is never reinterpreted.
    readonly property string timeText: now.toLocaleTimeString(Qt.locale(i18n.qtLocale), "HH:mm")
    readonly property string dateText: now.toLocaleDateString(Qt.locale(i18n.qtLocale), "ddd, d MMM")
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
    color: "transparent"
    Accessible.role: Accessible.Pane
    Accessible.name: hasWeather
        ? qsTr("%1, %2. %3, %4 %5, reading at %6.").arg(timeText).arg(dateText).arg(wx.district)
              .arg(temperatureText).arg(conditionLabel).arg(readingText)
        : qsTr("%1, %2, Local time, %3. Weather unavailable.").arg(timeText).arg(dateText).arg(zoneText)
    // The strip's top rule: it belongs to the sidebar, not a card of its own.
    Rectangle { width: parent.width; height: 1; color: Theme.glassSeparator }

    readonly property int lineY: Math.round(height / 2) + 1
    Text {
        id: clockText
        objectName: "contextClock"
        x: 4; anchors.verticalCenter: parent.top; anchors.verticalCenterOffset: context.lineY
        text: context.timeText; color: Theme.text
        font: Theme.clock(context.big ? 17 : 16)
    }
    Text {
        id: dateLabel
        objectName: "contextDate"
        x: clockText.x + clockText.width + 10; anchors.baseline: clockText.baseline
        text: context.dateText; color: Theme.textLabel; font: Theme.dateText; renderType: Theme.textRender
    }
    // The right part, in the order it gives way: with a reading the
    // temperature stays, then the condition, then the place; without one the
    // zone stays, then "no weather". What is not shown is in the tip.
    readonly property real room: width - 4 - (dateLabel.x + dateLabel.width + 12)
    FontMetrics { id: rightMetrics; font: Theme.caption }
    readonly property real tempW: hasWeather ? markItem.width + 6 + temperatureLabel.implicitWidth : 0
    readonly property bool showCondition: (hasWeather ? tempW + 8 : zoneLabel.implicitWidth + 8)
                                          + rightMetrics.advanceWidth(conditionLabelText.text) <= room
    readonly property bool showPlace: hasWeather && showCondition
                                      && tempW + 8 + rightMetrics.advanceWidth(conditionLabelText.text) + 8 + rightMetrics.advanceWidth(placeLabel.text) <= room
    Row {
        id: right
        objectName: "contextWeather"
        anchors { right: parent.right; rightMargin: 4; verticalCenter: parent.top; verticalCenterOffset: context.lineY }
        spacing: 8
        Item {
            id: local
            objectName: "contextLocation"
            visible: !context.hasWeather || context.showPlace
            width: context.hasWeather ? placeLabel.width : zoneLabel.width
            height: zoneLabel.height
            Text { id: placeLabel; objectName: "contextPlace"; renderType: Theme.textRender
                   visible: context.hasWeather
                   text: context.hasWeather ? context.wx.district : qsTr("Local time")
                   color: Theme.textLabel; font: Theme.caption }
            // With a reading: when the provider says it applies ("last reading" once it is no longer current).
            Text { id: zoneLabel; objectName: "contextTimeZone"; renderType: Theme.textRender
                   visible: !context.hasWeather
                   text: !context.hasWeather ? context.zoneText
                         : context.stale ? qsTr("Last reading %1").arg(context.readingText) : qsTr("As of %1").arg(context.readingText)
                   color: Theme.textMuted; font: Theme.caption }
        }
        Item {
            id: markItem
            objectName: "contextWeatherMark"
            visible: context.hasWeather
            property string family: context.family
            anchors.verticalCenter: parent.verticalCenter
            width: 16; height: 16
            opacity: context.stale ? 0.55 : 1
            Rectangle { anchors.fill: parent; radius: width / 2; color: "#2036aaff"; border.color: "#596d9ebe" }
            Rectangle { visible: markItem.family === "sun"; anchors.centerIn: parent; width: 6; height: 6; radius: 3; color: "#d8ecfa" }
            Column {
                visible: markItem.family === "cloud"; anchors.centerIn: parent; spacing: 2
                Repeater { model: [8, 6, 8]
                    Rectangle { anchors.horizontalCenter: parent.horizontalCenter; width: modelData; height: 1.4; radius: 0.7; color: "#c2dff4" } }
            }
            Row {
                visible: markItem.family === "fall"; anchors.centerIn: parent; spacing: 2
                Repeater { model: 3
                    Rectangle { width: 1.4; height: 5; radius: 0.7; rotation: 18; color: "#c2dff4" } }
            }
        }
        Text {
            id: temperatureLabel
            objectName: "contextTemperature"
            visible: context.hasWeather
            text: context.temperatureText; color: context.stale ? Theme.textMuted : Theme.text
            font: Theme.captionStrong; renderType: Theme.textRender
        }
        Text {
            id: conditionLabelText
            objectName: "contextWeatherCondition"
            renderType: Theme.textRender
            visible: context.showCondition
            // Unavailable: said whole, on one line.
            text: context.hasWeather ? context.conditionLabel : qsTr("No\nweather").replace("\n", " ")
            color: context.stale ? Theme.textMuted : Theme.textLabel; font: Theme.caption
        }
    }
    HoverTip { objectName: "contextTip"; text: context.Accessible.name }
}
