import QtQuick
import "../theme"

// Gate 6G U3 (G6U1A): the usage popover - "Plan usage limits · <provider>" and
// one row per window the provider REPORTED (5-hour, weekly): the used share,
// when it resets (the window's one date formatter) and a bar filled to exactly
// that share. A window not reported is not drawn - never a 0; none reported
// says so. Nothing is estimated. No credits or breakdown rows; the context-window
// block is V2.1 (Salih 2026-10-05). K4: one MenuSurface.
//
// Bridge-independent: `rows` are the card's quota rows as they are,
// [{window: "five_hour" | "weekly", remaining: 0-100, resets_at: epoch s, 0 = none}].
MenuSurface {
    id: pop
    objectName: "usagePopover"
    property string provider: ""
    property var rows: []
    property real now: Date.now()               // the clock reset times read against
    takesFocus: false
    width: 360; padding: 14
    onAboutToShow: now = Date.now()

    // A window's used % as reported (0-100), or null when it was not reported.
    function used(window) {
        for (let i = 0; i < rows.length; ++i)
            if (rows[i] && rows[i].window === window && typeof rows[i].remaining === "number" && isFinite(rows[i].remaining))
                return Math.max(0, Math.min(100, 100 - rows[i].remaining))
        return null
    }
    function resetsAt(window) {
        for (let i = 0; i < rows.length; ++i)
            if (rows[i] && rows[i].window === window) return Number(rows[i].resets_at) || 0
        return 0
    }
    readonly property var shown: [["five_hour", qsTr("5-hour session")], ["weekly", qsTr("Weekly")]]
        .filter(w => used(w[0]) !== null)
        .map(w => ({ window: w[0], title: w[1], used: used(w[0]), resets_at: resetsAt(w[0]) }))

    Column {
        width: pop.availableWidth
        spacing: 12
        Text { objectName: "usagePopoverTitle"; width: parent.width; wrapMode: Text.Wrap
               text: qsTr("Plan usage limits · %1").arg(pop.provider)
               color: Theme.textMuted; renderType: Theme.textRender; font: Theme.caption }
        Repeater {
            model: pop.shown
            Column {
                required property var modelData
                objectName: "usageRow_" + modelData.window
                width: parent.width; spacing: 6
                Item { width: parent.width; height: title.height
                    Text { id: title; objectName: "usageRowTitle"; text: modelData.title
                           color: Theme.text; renderType: Theme.textRender; font: Theme.captionStrong }
                    Row { anchors.right: parent.right; anchors.baseline: title.baseline; spacing: 8
                        Text { objectName: "usageRowReset"; visible: modelData.resets_at > 0
                               text: visible ? qsTr("Resets %1").arg(i18n.when(modelData.resets_at, pop.now, "time")) : ""
                               color: Theme.textMuted; renderType: Theme.textRender; font: Theme.caption }
                        Text { objectName: "usageRowUsed"; text: qsTr("%1%").arg(Math.round(modelData.used))
                               color: Theme.text; renderType: Theme.textRender; font: Theme.captionFigures } } }
                Rectangle { width: parent.width; height: 5; radius: 2.5; color: Theme.alpha(Theme.accent, 0.16)
                    Rectangle { objectName: "usageRowBar"; width: parent.width * modelData.used / 100
                                height: parent.height; radius: parent.radius; color: Theme.accent } }
            }
        }
        Text { objectName: "usageNone"; visible: pop.shown.length === 0; width: parent.width; wrapMode: Text.Wrap
               text: qsTr("This provider reported no usage limits.")
               color: Theme.textMuted; renderType: Theme.textRender; font: Theme.body }
        Text { objectName: "usageFootnote"; width: parent.width; wrapMode: Text.Wrap
               text: qsTr("Values as the provider reported them; nothing unreported is estimated.")
               color: Theme.alpha(Theme.textMuted, 0.75); renderType: Theme.textRender; font: Theme.caption }
    }
}
