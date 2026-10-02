import QtQuick
import QtQuick.Controls.Basic
import "../theme"
import "../components"

// Provider -> Model -> Effort -> Usage, from what each provider's own local
// tooling REPORTED (bridge.providers, no model call) and what the last real
// call reported. Nothing is estimated and nothing is offered that the route
// check did not establish.
//
// Provider and model are collapsed: at rest the panel shows only what is
// selected, and the choices appear when asked for. A choice is persisted the
// moment it is made and applies to the NEXT call - a call already running
// keeps the provider, model and effort it started with, and is never
// cancelled because the selection changed. The provider changes who answers
// and interprets a request, never what JARVIS may do.
//
// Usage is shown for the provider being looked at and for no other; the two
// primary rows carry what matters (who answered, what the plan has left), the
// small print sits beneath them. Google keeps its connection state and its
// quota code, but no quota control sits in this panel.
ContextPanel {
    id: modelPanel
    heading: qsTr("MODEL & PROVIDER")
    surfaceName: "modelSurface"
    surfaceFill: Theme.glassOverlay
    panelWidth: 460; panelHeight: 720
    onOpenChanged: if (open) { claudeDraft.model = bridge.config.claude_model || ""; claudeDraft.effort = bridge.config.claude_effort || ""
                               providerField.open = false; claudeField.open = false; codexField.open = false
                               bridge.refreshProviders() }

    function provider(id) { return (bridge.providers || []).filter(function(p) { return p.id === id })[0] || null }
    readonly property var claude: provider("claude")
    readonly property var codex: provider("codex")
    readonly property string chosen: bridge.chatProvider
    readonly property var usage: bridge.chatUsage || ({})
    readonly property var reported: usage.reported || ({})
    readonly property int calls: usage.call_count > 1 ? usage.call_count : 1
    function tokens(value) { return value === undefined ? "—" : Number(value).toLocaleString(Qt.locale(i18n.qtLocale), "f", 0) }
    function when(epoch) { return epoch ? new Date(epoch * 1000).toLocaleString(Qt.locale(i18n.qtLocale), Locale.ShortFormat) : "" }
    function percent(fraction) { return Math.round(fraction * 100) }
    function selectable(p) { return !!p && (p.id === "claude" || p.id === "codex") && p.status === "available" }
    function statusText(p) {
        if (!p) return qsTr("Checking…")
        switch (p.status) {
        case "available": return p.id === "codex" ? qsTr("ChatGPT subscription sign-in verified%1").arg(p.plan ? " · " + p.plan : "")
                                                  : qsTr("Subscription route verified")
        case "not_logged_in": return p.id === "claude" ? qsTr("Not signed in - run: %1").arg("claude auth login")
                               : p.id === "codex" ? qsTr("Not signed in - run: %1").arg("codex login")
                               : qsTr("Not signed in - run %1 and choose Sign in with Google").arg("gemini")
        case "not_installed": return qsTr("CLI not installed")
        case "unavailable": if (p.id === "google") return qsTr("Status unavailable - chat unavailable for safety")
                            return qsTr("Route not established - calls are refused")
        case "api_key_route": return qsTr("Set up with an API key (metered) - refused")
        case "chat_unavailable": return qsTr("Connected - chat unavailable for safety")
        case "deferred": return qsTr("Planned after V1")
        default: return qsTr("Route not established - calls are refused")
        }
    }
    readonly property var claudeModels: claude ? claude.models || [] : []
    readonly property var codexModels: codex ? codex.models || [] : []
    // An alias row is applied by the model id it RESOLVES to: the CLI's short alias does not always get that model
    // (live: "haiku" was answered by claude-sonnet-5, "claude-haiku-4-5-20251001" by Haiku). A full model id
    // row is applied as listed, so a variant it names ("[1m]": the long context) is never dropped.
    function claudeValue(m) { return m.id === "default" ? "" : m.id.indexOf("claude-") === 0 ? m.id : (m.resolved || m.id) }
    function claudeEntry(id) { var key = id || "default"; return claudeModels.filter(function(m) { return m.id === key || (id && m.resolved === id) })[0] || null }
    // S5: only the newest version per model name is offered (display only; the lists above stay complete).
    // ``bridge.modelLists`` names what is shown and which newer row replaces a hidden one.
    readonly property var lists: bridge.modelLists || ({})
    function listed(pid, id) { var l = lists[pid]; return !l || l.shown.indexOf(id) >= 0 }
    function keeperOf(pid, id) { var l = lists[pid]; return l && id && l.hidden[id] ? l.hidden[id] : "" }
    function codexEntry(id) { return codexModels.filter(function(m) { return m.id === id })[0] || null }
    // The short display name; the canonical id never changes with it.
    function shortName(id, fallback) { return bridge.modelLabel(id || "", fallback || "") }
    // Claude's choice: drafted here, persisted the moment it is made.
    QtObject { id: claudeDraft; property string model: ""; property string effort: "" }
    // ``row``: the catalog row it was chosen from; the bridge keeps it as the model's lane when it is an alias.
    function applyClaude(model, effort, row) { claudeDraft.model = model; claudeDraft.effort = effort; bridge.applyModel(model, effort, row || "") }

    // -- the panel's few text roles ------------------------------------------
    // A section name: quiet, sentence case, no tracking.
    component Caption: Text { color: Theme.textMuted; renderType: Theme.textRender; font: Theme.caption }
    // Small print: what is secondary lives here, readable but quiet.
    component Line: Text { width: parent ? parent.width : 0; wrapMode: Text.Wrap; color: Theme.textMuted; renderType: Theme.textRender; font: Theme.caption }
    // One of the two primary usage rows: a label and a large, clear value. The
    // plan figure is the dominant one (`big`: the instrument numerals); its
    // reset time is the quieter note beneath. A low remainder is attention,
    // amber - never red: nothing has failed.
    component Fact: Item {
        property string label: ""
        property string value: ""
        property string note: ""
        property bool big: false
        property bool attention: false
        property color valueColor: attention ? Theme.warning : "#e8f4ff"
        width: parent ? parent.width : 0
        height: 22 + valueText.height + (noteText.visible ? noteText.height + 2 : 0)
        Text { text: label; color: Theme.textMuted; renderType: Theme.textRender; font: Theme.caption }
        Text { id: valueText; renderType: Theme.textRender; objectName: parent.objectName ? parent.objectName + "Value" : ""
               y: 19; width: parent.width; elide: Text.ElideRight; text: value; color: valueColor
               font: big ? Qt.font({ family: Theme.display, pixelSize: 26, weight: Font.Medium, variableAxes: Theme.semibold600, features: Theme.tabular })
                         : Theme.title }
        Text { id: noteText; renderType: Theme.textRender; visible: note !== ""; y: valueText.y + valueText.height + 2; width: parent.width
               wrapMode: Text.Wrap; maximumLineCount: 2; elide: Text.ElideRight
               text: note; color: Theme.textLabel; font: Theme.caption }
    }
    // A compact state row: name on the left, a semantic badge on the right.
    component StateRow: Item {
        property string label: ""
        property bool on: false
        property string onText: qsTr("Available", "capability")
        property string offText: qsTr("Unavailable", "capability")
        width: parent ? parent.width : 0; height: 26
        Text { anchors.verticalCenter: parent.verticalCenter; text: label; color: Theme.textLabel
               font: Theme.body }
        Pill { objectName: parent.objectName ? parent.objectName + "Badge" : ""
               anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
               text: on ? onText : offText; tint: on ? Theme.success : Theme.textMuted }
    }
    // "Eski modelleri göster": window-only, off at every launch.
    component OldModelsToggle: Item {
        objectName: "showOldModelsToggle"
        width: parent ? parent.width : 0; height: 28
        Accessible.role: Accessible.CheckBox; Accessible.name: label.text; Accessible.checked: bridge.showOldModels
        Rectangle { id: box; x: 2; anchors.verticalCenter: parent.verticalCenter; width: 16; height: 16; radius: 4
                    color: bridge.showOldModels ? Theme.alpha(Theme.commandEdge, 0.25) : "transparent"
                    border.width: 1; border.color: bridge.showOldModels ? Theme.commandEdge : Theme.borderStrong
                    Text { anchors.centerIn: parent; visible: bridge.showOldModels; text: "✓"; color: Theme.commandEdge; font: Theme.caption } }
        Text { id: label; x: box.x + box.width + 8; anchors.verticalCenter: parent.verticalCenter
               text: qsTr("Show older models"); color: Theme.textLabel; font: Theme.caption }
        MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: bridge.setShowOldModels(!bridge.showOldModels) }
    }
    // A selected model that has a newer version: offered, never applied on its own.
    component NewerHint: Rectangle {
        property string keeperTitle: ""
        signal chosen()
        objectName: "modelNewerHint"
        width: parent ? parent.width : 0; height: 30; radius: Theme.radiusSmall
        color: hintArea.containsMouse ? Theme.alpha(Theme.commandEdge, 0.12) : Theme.alpha(Theme.commandEdge, 0.05)
        border.width: 1; border.color: Theme.alpha(Theme.commandEdge, 0.4)
        Accessible.role: Accessible.Button; Accessible.name: hintText.text
        Text { id: hintText; objectName: "modelNewerHintText"; x: 12; anchors.verticalCenter: parent.verticalCenter; width: parent.width - 24
               elide: Text.ElideRight; textFormat: Text.PlainText; color: Theme.text; font: Theme.caption
               text: qsTr("New version: %1").arg(parent.keeperTitle) }
        MouseArea { id: hintArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: parent.chosen() }
    }
    component Choice: Rectangle {
        id: choice
        property string title: ""
        property string meta: ""
        property bool selected: false
        property bool selectable: true
        signal chosen()
        width: parent ? parent.width : 0; height: 50; radius: Theme.radiusSmall
        opacity: selectable ? 1 : 0.55
        // Three distinct states of the BACKGROUND: hover is a small tonal lift,
        // selected is the stable fill with its edge mark, keyboard focus is the
        // crisp outline - never colour alone for any of them.
        color: area.containsMouse && selectable ? Theme.alpha(Theme.commandEdge, 0.08) : selected ? Theme.alpha(Theme.commandEdge, 0.06) : "transparent"
        border.width: 1
        border.color: selected ? Theme.alpha(Theme.commandEdge, 0.45) : Theme.alpha(Theme.text, 0.05)
        activeFocusOnTab: selectable
        Accessible.role: Accessible.Button
        Accessible.name: title
        Keys.onReturnPressed: if (selectable) chosen()
        Keys.onSpacePressed: if (selectable) chosen()
        MouseArea { id: area; anchors.fill: parent; hoverEnabled: true; enabled: choice.selectable
                    cursorShape: Qt.PointingHandCursor; onClicked: choice.chosen() }
        Rectangle { anchors.fill: parent; anchors.margins: 1; radius: parent.radius - 1; color: "transparent"
                    border.width: 2; border.color: Theme.materialGlint; visible: choice.activeFocus }
        // The chosen row: a fine cool light along its left edge, and its name in
        // clean white-cyan. Restrained - a mark, not a glow.
        Rectangle { visible: choice.selected; x: 0; y: 8; width: 2; height: parent.height - 16; radius: 1; color: Theme.commandEdge; opacity: 0.85 }
        Column { x: 14; anchors.verticalCenter: parent.verticalCenter; width: parent.width - 52; spacing: 2
            Text { width: parent.width; elide: Text.ElideRight; text: choice.title
                   color: choice.selected ? "#e6f8ff" : Theme.text
                   font: Theme.face(16, choice.selected) }
            Text { width: parent.width; elide: Text.ElideRight; text: choice.meta; color: Theme.textMuted; font: Theme.caption } }
        Text { visible: choice.selected; anchors.verticalCenter: parent.verticalCenter; x: parent.width - 26; text: "✓"; color: Theme.commandEdge
               font: Theme.subtitle }
    }

    Flickable {
        anchors.fill: parent
        contentHeight: col.height + 8
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded; width: 6 }
        Column {
            id: col
            width: parent.width - 10; spacing: 12

            // -- 1. Provider --------------------------------------------------
            Caption { text: qsTr("Provider") }
            Collapsed {
                id: providerField
                objectName: "providerField"
                readonly property var current: modelPanel.provider(modelPanel.chosen)
                title: current ? current.name + "  ·  " + current.vendor : qsTr("Checking the providers…")
                meta: modelPanel.statusText(current)
                Repeater {
                    model: bridge.providers || []
                    Choice {
                        objectName: "providerRow_" + modelData.id
                        title: modelData.name + "  ·  " + modelData.vendor
                        meta: modelPanel.statusText(modelData)
                        selectable: modelPanel.selectable(modelData)
                        selected: modelData.id === modelPanel.chosen
                        onChosen: {
                            bridge.setChatProvider(modelData.id)
                            if (modelData.id === "codex" && !bridge.codexModel && (modelData.models || []).length > 0)
                                bridge.setCodexModel(modelData.models[0].id, modelData.models[0].default_effort || "")
                            providerField.open = false
                        }
                    }
                }
            }
            Line { objectName: "providerAuthorityNote"; color: Theme.textDim
                   text: qsTr("The provider answers and interprets requests; it never changes what JARVIS may do. Manual choice, no automatic fallback.") }
            Rectangle { width: parent.width; height: 1; color: Theme.border }

            // -- Claude: 2. Model, 3. Effort, 4. Usage ---------------------------
            Column {
                visible: modelPanel.chosen === "claude"
                width: parent.width; spacing: 12
                Caption { text: qsTr("Model") }
                Collapsed {
                    id: claudeField
                    objectName: "claudeModelField"
                    title: claudeDraft.model ? modelPanel.shortName(claudeDraft.model, claudeDraft.model) : qsTr("CLI default")
                    meta: claudeDraft.model ? claudeDraft.model
                          : modelPanel.claudeEntry("") && modelPanel.claudeEntry("").resolved
                            ? qsTr("Currently %1").arg(modelPanel.claudeEntry("").resolved) : qsTr("Whatever the Claude Code CLI selects")
                    Choice {
                        objectName: "modelRowDefault"
                        title: qsTr("CLI default")
                        meta: modelPanel.claudeEntry("") && modelPanel.claudeEntry("").resolved ? qsTr("Currently %1").arg(modelPanel.claudeEntry("").resolved) : qsTr("Whatever the Claude Code CLI selects")
                        selected: claudeDraft.model === ""
                        onChosen: { modelPanel.applyClaude("", claudeDraft.effort); claudeField.open = false }
                    }
                    Repeater {
                        model: modelPanel.claudeModels.filter(function(m) { return m.id !== "default" && modelPanel.listed("claude", m.id) })
                        Choice {
                            objectName: "claudeModel_" + modelData.id
                            title: modelPanel.shortName(modelPanel.claudeValue(modelData), modelData.name)
                            meta: modelPanel.claudeValue(modelData)
                            selected: claudeDraft.model === modelPanel.claudeValue(modelData) || claudeDraft.model === modelData.id
                            onChosen: {
                                var keep = claudeDraft.effort && modelData.efforts.indexOf(claudeDraft.effort) >= 0 ? claudeDraft.effort : ""
                                modelPanel.applyClaude(modelPanel.claudeValue(modelData), keep, modelData.id)
                                claudeField.open = false
                            }
                        }
                    }
                }
                NewerHint {
                    readonly property string keeper: modelPanel.keeperOf("claude", claudeDraft.model)
                    readonly property var row: modelPanel.claudeModels.filter(function(m) { return m.id === keeper })[0] || null
                    visible: !!row; height: visible ? 30 : 0
                    keeperTitle: row ? modelPanel.shortName(modelPanel.claudeValue(row), row.name) : ""
                    onChosen: {
                        var keep = claudeDraft.effort && row.efforts.indexOf(claudeDraft.effort) >= 0 ? claudeDraft.effort : ""
                        modelPanel.applyClaude(modelPanel.claudeValue(row), keep, row.id)
                    }
                }
                OldModelsToggle { }
                Line { visible: modelPanel.claude && modelPanel.claude.status === "available" && modelPanel.claudeModels.length === 0
                       text: qsTr("The CLI did not list its models.") }
                Caption { text: qsTr("Effort") }
                Segmented {
                    objectName: "claudeEffort"
                    readonly property var entry: modelPanel.claudeEntry(claudeDraft.model)
                    readonly property var levels: entry ? entry.efforts : bridge.effortChoices
                    enabled: levels.length > 0
                    value: claudeDraft.effort
                    onPicked: function(v) { modelPanel.applyClaude(claudeDraft.model, v) }
                    options: [{ value: "", label: qsTr("Default") }].concat(levels.map(function(e) { return { value: e, label: e } }))
                }
                Line { visible: !!modelPanel.claudeEntry(claudeDraft.model) && modelPanel.claudeEntry(claudeDraft.model).efforts.length === 0
                       text: qsTr("This model does not support an effort setting.") }
                Rectangle { width: parent.width; height: 1; color: Theme.border }
                Caption { text: qsTr("Usage") }
                // The two primary rows: who really answered, and what the plan has left.
                Fact {
                    objectName: "actualModelLine"
                    label: qsTr("Answered the last call")
                    value: modelPanel.usage.provider === "anthropic" && modelPanel.reported.model
                           ? modelPanel.shortName(modelPanel.reported.model, modelPanel.reported.model) : qsTr("No Claude call yet")
                    note: modelPanel.usage.provider === "anthropic" && modelPanel.reported.model
                          ? modelPanel.reported.model + (modelPanel.calls > 1 ? "  ·  " + qsTr("%1 calls, one per group").arg(modelPanel.calls) : "") : ""
                }
                Fact {
                    objectName: "claudePlanFact"
                    readonly property var five: ((bridge.claudeLimits || {}).windows || {}).five_hour
                    readonly property bool known: !!five && five.utilization !== undefined
                    label: qsTr("Plan, this session")
                    big: known
                    attention: known && 100 - modelPanel.percent(five.utilization) <= 25
                    value: known ? qsTr("%1% left").arg(100 - modelPanel.percent(five.utilization)) : qsTr("Not reported yet")
                    note: five && five.resets_at ? qsTr("Resets %1").arg(modelPanel.when(five.resets_at)) : ""
                }
                Repeater {
                    model: Object.keys((bridge.claudeLimits || {}).windows || {})
                    QuotaBar {
                        objectName: "claudeQuota_" + modelData
                        readonly property var w: bridge.claudeLimits.windows[modelData]
                        label: modelData === "five_hour" ? qsTr("Current session (5 h)")
                               : modelData === "seven_day" ? qsTr("Current week (all models)")
                               : modelData === "seven_day_opus" ? qsTr("Current week (Opus)")
                               : modelData === "seven_day_sonnet" ? qsTr("Current week (Sonnet)") : modelData
                        remaining: 100 - modelPanel.percent(w.utilization)
                        resetText: modelPanel.when(w.resets_at)
                    }
                }
                StateRow { objectName: "claudeResearchState"; label: qsTr("Web Research"); on: !!modelPanel.claude && modelPanel.claude.research === true }
                // Small print, beneath what matters.
                Column {
                    width: parent.width; spacing: 4
                    Line { objectName: "configuredModelLine"
                           text: qsTr("Configured model: %1 · effort: %2").arg(bridge.config.claude_model || qsTr("CLI default")).arg(bridge.config.claude_effort || qsTr("Default")) }
                    Line { objectName: "quotaLine"
                           text: Object.keys(bridge.claudeLimits || {}).length === 0
                                 ? qsTr("Plan limits: shown after the first Claude call - Claude Code reports them with each answer, and JARVIS makes no call just to check.")
                                 : qsTr("As reported by Claude Code at %1 · extra paid usage: %2").arg(modelPanel.when(bridge.claudeLimits.seen_at))
                                       .arg(bridge.claudeLimits.using_overage === true ? qsTr("IN USE") : bridge.claudeLimits.overage_status === "rejected" ? qsTr("off") : qsTr("not reported")) }
                    Line { objectName: "usageLine"
                           text: modelPanel.usage.provider !== "anthropic" || Object.keys(modelPanel.reported).length === 0 ? qsTr("Last call: no usage reported yet")
                                 : (modelPanel.calls > 1 ? qsTr("Last turn: %1 calls, one per group. Tokens summed over all of them:").arg(modelPanel.calls) + " " : "")
                                   + qsTr("Last call tokens: %1 input · %2 cache read · %3 cache write · %4 output")
                                     .arg(modelPanel.tokens(modelPanel.reported.input_tokens))
                                     .arg(modelPanel.tokens(modelPanel.reported.cache_read_input_tokens))
                                     .arg(modelPanel.tokens(modelPanel.reported.cache_creation_input_tokens))
                                     .arg(modelPanel.tokens(modelPanel.reported.output_tokens)) }
                }
            }

            // -- Codex: 2. Model, 3. Effort, 4. Usage ----------------------------
            Column {
                id: codexBlock
                visible: modelPanel.chosen === "codex"
                width: parent.width; spacing: 12
                readonly property var current: modelPanel.codexEntry(bridge.codexModel)
                Caption { text: qsTr("Model") }
                Collapsed {
                    id: codexField
                    objectName: "codexModelField"
                    title: bridge.codexModel ? modelPanel.shortName(bridge.codexModel, bridge.codexModel) : qsTr("No model chosen")
                    meta: bridge.codexModel ? bridge.codexModel : qsTr("Choose one of the models Codex lists")
                    Repeater {
                        model: modelPanel.codexModels.filter(function(m) { return modelPanel.listed("codex", m.id) })
                        Choice {
                            objectName: "codexModel_" + modelData.id
                            title: modelPanel.shortName(modelData.id, modelData.name)
                            // Listed by Codex is not proof it works: say what a real call showed.
                            readonly property var verified: modelData.verified || ({})
                            meta: modelData.id + "  ·  " + (verified.state === "usable" ? qsTr("Answered this session")
                                   : verified.state === "failed" ? qsTr("Failed this session: %1").arg(verified.reason)
                                   : qsTr("Listed by Codex - not tried yet"))
                            selected: bridge.codexModel === modelData.id
                            onChosen: {
                                bridge.setCodexModel(modelData.id, modelData.efforts.indexOf(bridge.codexEffort) >= 0 ? bridge.codexEffort : modelData.default_effort)
                                codexField.open = false
                            }
                        }
                    }
                }
                NewerHint {
                    readonly property string keeper: modelPanel.keeperOf("codex", bridge.codexModel)
                    readonly property var row: modelPanel.codexEntry(keeper)
                    visible: !!row; height: visible ? 30 : 0
                    keeperTitle: row ? modelPanel.shortName(row.id, row.name) : ""
                    onChosen: bridge.setCodexModel(row.id, row.efforts.indexOf(bridge.codexEffort) >= 0 ? bridge.codexEffort : row.default_effort)
                }
                OldModelsToggle { }
                Line { visible: !!modelPanel.codex && modelPanel.codex.status === "available" && modelPanel.codexModels.length === 0
                       text: qsTr("Codex listed no model that JARVIS can run with every tool disabled.") }
                Line { visible: !!bridge.codexModel && !codexBlock.current && modelPanel.codexModels.length > 0
                       text: qsTr("The saved model %1 is not offered now; calls are refused until you choose one.").arg(bridge.codexModel) }
                Caption { text: qsTr("Effort") }
                Segmented {
                    objectName: "codexEffort"
                    enabled: !!codexBlock.current
                    value: bridge.codexEffort
                    onPicked: function(v) { bridge.setCodexModel(bridge.codexModel, v) }
                    options: (codexBlock.current ? codexBlock.current.efforts : []).map(function(e) { return { value: e, label: e } })
                }
                Rectangle { width: parent.width; height: 1; color: Theme.border }
                Caption { text: qsTr("Usage") }
                Fact {
                    objectName: "codexActualModelLine"
                    label: qsTr("Answering model")
                    value: bridge.codexModel ? modelPanel.shortName(bridge.codexModel, bridge.codexModel) : qsTr("No model chosen")
                    note: qsTr("Codex does not report it - the call requests %1").arg(bridge.codexModel || qsTr("none"))
                          + (modelPanel.usage.provider === "openai" && modelPanel.calls > 1 ? "  ·  " + qsTr("%1 calls, one per group").arg(modelPanel.calls) : "")
                }
                Fact {
                    objectName: "codexPlanFact"
                    readonly property var primary: (bridge.codexQuota || {}).primary
                    readonly property bool known: !!primary && primary.used_percent !== undefined
                    label: qsTr("Plan, this session")
                    big: known
                    attention: known && 100 - Math.round(primary.used_percent) <= 25
                    value: known ? qsTr("%1% left").arg(100 - Math.round(primary.used_percent)) : qsTr("Not reported yet")
                    note: primary && primary.resets_at ? qsTr("Resets %1").arg(modelPanel.when(primary.resets_at)) : ""
                }
                Repeater {
                    model: ["primary", "secondary"].filter(function(k) { return !!(bridge.codexQuota || {})[k] })
                    QuotaBar {
                        objectName: "codexQuota_" + modelData
                        readonly property var w: bridge.codexQuota[modelData]
                        label: w.window_minutes === 300 ? qsTr("5-hour window") : w.window_minutes === 10080 ? qsTr("Weekly window")
                               : qsTr("%1-minute window").arg(w.window_minutes)
                        remaining: 100 - Math.round(w.used_percent)
                        resetText: modelPanel.when(w.resets_at)
                    }
                }
                StateRow { objectName: "codexResearchState"; label: qsTr("Web Research"); on: !!modelPanel.codex && modelPanel.codex.research === true }
                Column {
                    width: parent.width; spacing: 4
                    Line { text: qsTr("Configured model: %1 · effort: %2").arg(bridge.codexModel || qsTr("none")).arg(bridge.codexEffort || qsTr("model default")) }
                    Line { text: Object.keys(bridge.codexQuota || {}).length === 0 ? qsTr("Plan limits: not reported yet.")
                                 : qsTr("As reported by Codex at %1 · purchased credits: %2").arg(modelPanel.when(bridge.codexQuota.seen_at))
                                       .arg(bridge.codexQuota.has_credits === true ? qsTr("present") : bridge.codexQuota.has_credits === false ? qsTr("none") : qsTr("not reported")) }
                    Line { text: modelPanel.usage.provider !== "openai" || Object.keys(modelPanel.reported).length === 0 ? qsTr("Last call: no usage reported yet")
                                 : (modelPanel.calls > 1 ? qsTr("Last turn: %1 calls, one per group. Tokens summed over all of them:").arg(modelPanel.calls) + " " : "")
                                   + qsTr("Last call tokens: %1 input · %2 cached · %3 output · %4 reasoning")
                                     .arg(modelPanel.tokens(modelPanel.reported.input_tokens)).arg(modelPanel.tokens(modelPanel.reported.cached_input_tokens))
                                     .arg(modelPanel.tokens(modelPanel.reported.output_tokens)).arg(modelPanel.tokens(modelPanel.reported.reasoning_output_tokens)) }
                }
            }
        }
    }
}
