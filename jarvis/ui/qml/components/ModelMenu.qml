import QtQuick
import "../theme"

// Gate 6G M3 (G6CM1): the "Model ⌄" chip under the composer and its mini menu.
// Top: the selected model; then the models each provider's CLI REPORTED
// (bridge.providers), grouped under their brand - one click chooses the model
// AND its provider. "More models ›" lists the older versions (bridge.modelLists'
// hidden set), grouped the same way. No catalog is written here.
//
// A UI re-layout only: a choice goes through the same bridge calls the
// MODEL & PROVIDER panel makes (applyModel / setCodexModel / setChatProvider),
// is persisted at once and applies to the NEXT call. The selected model's effort
// facts are exposed for the Effort chip beside it.
Item {
    id: chip
    objectName: "modelMenuChip"
    implicitWidth: face.width; implicitHeight: 30
    width: implicitWidth; height: implicitHeight
    enabled: !bridge.configRefused
    opacity: enabled ? 1 : 0.5

    readonly property var config: bridge.config || ({})
    readonly property string chosen: bridge.chatProvider
    function provider(id) { return (bridge.providers || []).filter(function(p) { return p.id === id })[0] || null }
    function selectable(p) { return !!p && (p.id === "claude" || p.id === "codex") && p.status === "available" }
    // As the panel: an alias row is applied by the model it RESOLVES to, a full id as listed.
    function claudeValue(m) { return m.id === "default" ? "" : m.id.indexOf("claude-") === 0 ? m.id : (m.resolved || m.id) }
    function claudeEntry(id) { var key = id || "default"; return ((provider("claude") || {}).models || []).filter(function(m) { return m.id === key || (id && m.resolved === id) })[0] || null }
    function codexEntry(id) { return ((provider("codex") || {}).models || []).filter(function(m) { return m.id === id })[0] || null }
    function shortName(id, fallback) { return bridge.modelLabel(id || "", fallback || "") }
    // The chip's name: the top bar card's own rule (bridge.modelCard).
    readonly property var card: bridge.modelCard || ({})
    readonly property string currentName: card.name ? card.name : card.provider === "claude" ? qsTr("CLI default") : qsTr("No model chosen")

    // -- the selected model's effort facts (the Effort chip reads these) --------
    readonly property var entry: chosen === "codex" ? codexEntry(bridge.codexModel) : claudeEntry(config.claude_model || "")
    readonly property var levels: chosen === "codex" ? (entry ? entry.efforts || [] : [])
                                                     : (entry ? entry.efforts || [] : bridge.effortChoices || [])
    readonly property string effort: chosen === "codex" ? bridge.codexEffort || "" : config.claude_effort || ""
    readonly property string recommended: entry && entry.default_effort ? entry.default_effort : ""
    function setEffort(level) {
        if (chosen === "codex") bridge.setCodexModel(bridge.codexModel, level)
        else bridge.applyModel(config.claude_model || "", level, "")
    }

    // -- the rows ---------------------------------------------------------------
    readonly property var brands: [{ pid: "claude", label: "ANTHROPIC", title: "Anthropic" },
                                   { pid: "codex", label: "OPENAI · CODEX", title: "OpenAI" }]
    function rowsOf(pid, older) {
        var p = provider(pid)
        var hidden = ((bridge.modelLists || {})[pid] || {}).hidden || {}
        return ((p || {}).models || []).filter(function(m) { return hidden.hasOwnProperty(m.id) === older }).map(function(m) {
            var dflt = pid === "claude" && m.id === "default"
            return { pid: pid, model: m, available: selectable(p), isDefault: dflt,
                     title: dflt ? (m.resolved ? shortName(m.resolved, m.resolved) : qsTr("CLI default"))
                                 : pid === "claude" ? shortName(claudeValue(m), m.name) : shortName(m.id, m.name),
                     selected: chosen === pid && (pid === "codex" ? bridge.codexModel === m.id
                                                  : dflt ? !config.claude_model : config.claude_model === claudeValue(m) || config.claude_model === m.id) }
        })
    }
    function groups(older) {
        return brands.map(function(b) { return { label: b.label, rows: rowsOf(b.pid, older) } }).filter(function(g) { return g.rows.length > 0 })
    }
    readonly property var older: groups(true)
    function choose(row) {
        var m = row.model
        if (row.pid === "claude") {
            if (chosen !== "claude") bridge.setChatProvider("claude")
            var effortNow = config.claude_effort || ""
            if (row.isDefault) bridge.applyModel("", effortNow, "")
            else bridge.applyModel(claudeValue(m), effortNow && (m.efforts || []).indexOf(effortNow) >= 0 ? effortNow : "", m.id)
        } else {
            if (chosen !== "codex") bridge.setChatProvider("codex")
            bridge.setCodexModel(m.id, (m.efforts || []).indexOf(bridge.codexEffort) >= 0 ? bridge.codexEffort : (m.default_effort || ""))
        }
        moreMenu.close(); menu.close()
    }

    Accessible.role: Accessible.Button
    Accessible.name: qsTr("Model: %1").arg(currentName)
    Rectangle {
        id: face
        width: faceRow.width + 20; height: 30; radius: Theme.radiusSmall
        color: hit.containsMouse || menu.visible ? Theme.alpha(Theme.text, 0.08) : "transparent"
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
        Row { id: faceRow; anchors.centerIn: parent; spacing: 6
            Text { objectName: "modelMenuChipLabel"; anchors.verticalCenter: parent.verticalCenter
                   width: Math.min(implicitWidth, 180); elide: Text.ElideRight
                   text: chip.currentName; color: Theme.text; renderType: Theme.textRender; font: Theme.composerChip }
            Icon { anchors.verticalCenter: parent.verticalCenter; name: "chevron-down"; size: 12; tint: Theme.textMuted } }
        MouseArea { id: hit; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                    onClicked: menu.visible ? menu.close() : menu.open() }
    }

    component Heading: Text { width: parent ? parent.width : 0; leftPadding: 10; topPadding: 8; bottomPadding: 2
                              color: Theme.textMuted; renderType: Theme.textRender; font: Theme.label }
    component ModelRow: MenuRow {
        required property var modelData
        objectName: "modelMenuRow_" + modelData.pid + "_" + modelData.model.id
        Accessible.role: Accessible.RadioButton
        label: modelData.title
        shortcut: modelData.isDefault ? qsTr("Default") : ""
        checked: modelData.selected
        available: modelData.available
        onTriggered: chip.choose(modelData)
    }

    MenuSurface {
        id: menu
        objectName: "modelMenu"
        trigger: chip; takesFocus: false
        restY: -height - 8; x: chip.width - width
        width: 280; padding: 6
        onAboutToShow: bridge.refreshProviders()
        onClosed: moreMenu.close()
        Column {
            width: menu.availableWidth
            spacing: 1
            Heading { text: qsTr("SELECTED"); topPadding: 4 }
            MenuRow { objectName: "modelMenuCurrent"; label: chip.currentName; current: true
                      shortcut: chip.chosen === "codex" ? "OpenAI" : "Anthropic"
                      onTriggered: menu.close() }
            Repeater {
                model: chip.groups(false)
                Column {
                    required property var modelData
                    width: parent.width; spacing: 1
                    Rectangle { width: parent.width; height: 1; color: Theme.border }
                    Heading { text: modelData.label }
                    Repeater { model: modelData.rows; ModelRow { } }
                }
            }
            Rectangle { visible: chip.older.length > 0; width: parent.width; height: 1; color: Theme.border }
            MenuRow {
                id: moreRow
                objectName: "modelMenuMore"
                visible: chip.older.length > 0
                label: qsTr("More models"); shortcut: "›"
                current: moreMenu.visible
                onTriggered: moreMenu.visible ? moreMenu.close() : moreMenu.popupAt(moreRow, Qt.point(-moreMenu.width - 12, moreRow.height - moreMenu.implicitHeight), { trigger: moreRow })
            }
        }
    }
    // The older versions, beside the menu; selectable exactly like the newer ones.
    MenuSurface {
        id: moreMenu
        objectName: "moreModelsMenu"
        takesFocus: false
        width: 240; padding: 6
        Column {
            width: moreMenu.availableWidth
            spacing: 1
            Repeater {
                model: chip.older
                Column {
                    required property var modelData
                    width: parent.width; spacing: 1
                    Heading { text: modelData.label }
                    Repeater { model: modelData.rows; ModelRow { } }
                }
            }
        }
    }
}
