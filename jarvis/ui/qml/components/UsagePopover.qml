import QtQuick
import "../theme"

// Gate 6G U3 (G6U1A), reshaped by G6MP1 ("ModelPanel A", Salih 2026-10-10) after
// Claude's own plan-usage panel: "Plan usage limits · <plan>" (the plan only when
// the provider reported it) and one row per window the provider REPORTED -
// the title, when it resets ("in 2 h 41 min" under a day, else the weekday and
// time) and the used share, over a bar filled to exactly that share in the one
// accent colour. A window not reported is not drawn - never a 0; none reported
// says so. Beneath, "Details": what only the retired Model panel showed (plan
// facts, Web Research, extra paid usage, the last call's tokens), each line only
// when it was reported. Nothing is estimated. K4: one MenuSurface.
//
// Bridge-independent: `rows` are the quota rows as they are,
// [{window: "five_hour" | "weekly" | "weekly_<model>", remaining: 0-100,
//   resets_at: epoch s (0 = none), model: "Opus" (per-model weeks only)}];
// `plan` and `details` are bridge.usageLimits' plan / map.
MenuSurface {
    id: pop
    objectName: "usagePopover"
    surfaceName: "usageSurface"
    property string plan: ""
    property var rows: []
    property var details: ({})
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
    function rowTitle(row) {
        return row.window === "five_hour" ? qsTr("Session limit")
             : row.window === "weekly" ? qsTr("Weekly · all models")
             : qsTr("Weekly · %1").arg(row.model || row.window)
    }
    // Under a day: how long until it resets; later: the weekday and time (the window's locale).
    function resetText(at) {
        const left = (Number(at) || 0) - now / 1000
        if (!(Number(at) > 0) || left <= 0) return ""
        if (left < 86400) {
            const minutes = Math.max(1, Math.floor(left / 60))
            return minutes >= 60 ? qsTr("Resets in %1 h %2 min").arg(Math.floor(minutes / 60)).arg(minutes % 60)
                                 : qsTr("Resets in %1 min").arg(minutes)
        }
        return qsTr("Resets %1").arg(new Date(at * 1000).toLocaleString(Qt.locale(i18n.qtLocale), "ddd HH:mm"))
    }
    readonly property var shown: rows.filter(r => r && used(r.window) !== null)
        .map(r => ({ window: r.window, title: rowTitle(r), used: used(r.window), reset: resetText(r.resets_at) }))

    // The chosen provider's route as its own check reported it (the Model panel's wording).
    function routeText(provider, status) {
        switch (status) {
        case "available": return provider === "codex" ? qsTr("ChatGPT subscription sign-in verified") : qsTr("Subscription route verified")
        case "not_logged_in": return qsTr("Not signed in - run: %1").arg(provider === "codex" ? "codex login" : "claude auth login")
        case "not_installed": return qsTr("CLI not installed")
        case "api_key_route": return qsTr("Set up with an API key (metered) - refused")
        default: return qsTr("Route not established - calls are refused")
        }
    }
    function count(value) { return Number(value).toLocaleString(Qt.locale(i18n.qtLocale), "f", 0) }
    // [objectName suffix, label, value] - only what was reported.
    readonly property var detailRows: {
        const d = details || {}, out = []
        const last = d.last_call || {}, reported = last.reported || {}
        if (d.status)
            out.push(["Route", qsTr("Route"), routeText(d.provider, d.status)])
        if (d.verified)
            out.push(["ModelState", qsTr("Model"), (d.verified.model ? d.verified.model + " · " : "")
                      + (d.verified.state === "usable" ? qsTr("Answered this session")
                         : d.verified.state === "failed" ? qsTr("Failed this session: %1").arg(d.verified.reason || "")
                         : qsTr("Listed by Codex - not tried yet"))])
        if (typeof reported.model === "string" && reported.model)
            out.push(["Model", qsTr("Answered the last call"), reported.model])
        if (Number(d.seen_at) > 0)
            out.push(["Seen", qsTr("Reported"), i18n.when(d.seen_at, now, "time")])
        if (d.overage === "in_use" || d.overage === "off")
            out.push(["Overage", qsTr("Extra paid usage"), d.overage === "in_use" ? qsTr("In use") : qsTr("Off")])
        if (typeof d.credits === "boolean")
            out.push(["Credits", qsTr("Purchased credits"), d.credits ? qsTr("Present") : qsTr("None")])
        if (typeof d.research === "boolean")
            out.push(["Research", qsTr("Web Research"), d.research ? qsTr("Available") : qsTr("Unavailable")])
        const parts = [["input_tokens", qsTr("%1 input")], ["cache_read_input_tokens", qsTr("%1 cache read")],
                       ["cache_creation_input_tokens", qsTr("%1 cache write")], ["cached_input_tokens", qsTr("%1 cached")],
                       ["output_tokens", qsTr("%1 output")], ["reasoning_output_tokens", qsTr("%1 reasoning")]]
            .filter(p => typeof reported[p[0]] === "number" && isFinite(reported[p[0]]))
            .map(p => p[1].arg(count(reported[p[0]])))
        if (parts.length > 0)
            out.push(["Tokens", last.calls > 1 ? qsTr("Last turn tokens (%1 calls)").arg(last.calls) : qsTr("Last call tokens"), parts.join(" · ")])
        return out
    }

    Column {
        width: pop.availableWidth
        spacing: 12
        Text { objectName: "usagePopoverTitle"; width: parent.width; wrapMode: Text.Wrap
               text: pop.plan ? qsTr("Plan usage limits · %1").arg(pop.plan.charAt(0).toUpperCase() + pop.plan.slice(1)) : qsTr("Plan usage limits")
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
                    // On the title's baseline (a Row has none of its own: its top would land there).
                    Text { id: usedText; objectName: "usageRowUsed"; anchors.right: parent.right; anchors.baseline: title.baseline
                           text: qsTr("%1%").arg(Math.round(modelData.used))
                           color: Theme.text; renderType: Theme.textRender; font: Theme.captionFigures }
                    Text { objectName: "usageRowReset"; anchors.right: usedText.left; anchors.rightMargin: 8; anchors.baseline: title.baseline
                           visible: text !== ""; text: modelData.reset
                           color: Theme.textMuted; renderType: Theme.textRender; font: Theme.caption } }
                Rectangle { width: parent.width; height: 5; radius: 2.5; color: Theme.alpha(Theme.accent, 0.16)
                    Rectangle { objectName: "usageRowBar"; width: parent.width * modelData.used / 100
                                height: parent.height; radius: parent.radius; color: Theme.accent } }
            }
        }
        Text { objectName: "usageNone"; visible: pop.shown.length === 0; width: parent.width; wrapMode: Text.Wrap
               text: qsTr("This provider reported no usage limits.")
               color: Theme.textMuted; renderType: Theme.textRender; font: Theme.body }
        Column {
            objectName: "usageDetails"
            visible: pop.detailRows.length > 0
            width: parent.width; spacing: 6
            Rectangle { width: parent.width; height: 1; color: Theme.border }
            Text { text: qsTr("Details"); color: Theme.textMuted; renderType: Theme.textRender; font: Theme.caption }
            Repeater {
                model: pop.detailRows
                Item {
                    required property var modelData
                    objectName: "usageDetail" + modelData[0]
                    readonly property string label: modelData[1]
                    readonly property string value: modelData[2]
                    width: parent.width; height: Math.max(labelText.height, valueText.height)
                    Text { id: labelText; text: modelData[1]; color: Theme.textMuted; renderType: Theme.textRender; font: Theme.caption }
                    Text { id: valueText; objectName: "usageDetailValue"; anchors.right: parent.right
                           width: Math.min(implicitWidth, parent.width - labelText.width - 16)
                           wrapMode: Text.Wrap; horizontalAlignment: Text.AlignRight; textFormat: Text.PlainText
                           text: modelData[2]; color: Theme.text; renderType: Theme.textRender; font: Theme.caption }
                }
            }
        }
        Text { objectName: "usageFootnote"; width: parent.width; wrapMode: Text.Wrap
               text: qsTr("Values as the provider reported them; nothing unreported is estimated.")
               color: Theme.alpha(Theme.textMuted, 0.75); renderType: Theme.textRender; font: Theme.caption }
    }
}
