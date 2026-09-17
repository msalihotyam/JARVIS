import QtQuick
import QtQuick.Controls.Basic
import "../theme"
import "../components"

// Provider -> Model -> Effort -> Usage / Quota, from what each provider's own
// local tooling REPORTED (bridge.providers, no model call) and what the last
// real call reported. Nothing is estimated and nothing is offered that the
// route check did not establish.
//
// Provider and model are collapsed: at rest the panel shows only what is
// selected, and the choices appear when asked for. A choice is persisted the
// moment it is made and applies to the NEXT call - a call already running
// keeps the provider, model and effort it started with, and is never
// cancelled because the selection changed. The provider changes who answers
// and interprets a request, never what JARVIS may do.
//
// Quota is shown for the provider being looked at and for no other: Google's
// read-only pools live behind Google's own row and never appear while Claude
// or Codex is selected.
ContextPanel {
    id: modelPanel
    heading: qsTr("MODEL & PROVIDER")
    surfaceName: "modelSurface"
    surfaceFill: Theme.glassModelPanel
    panelWidth: 460; panelHeight: 720
    onOpenChanged: if (open) { claudeDraft.model = bridge.config.claude_model || ""; claudeDraft.effort = bridge.config.claude_effort || ""
                               modelField.text = ""; providerField.open = false; claudeField.open = false; codexField.open = false
                               bridge.refreshProviders() }

    function provider(id) { return (bridge.providers || []).filter(function(p) { return p.id === id })[0] || null }
    readonly property var claude: provider("claude")
    readonly property var codex: provider("codex")
    readonly property string chosen: bridge.chatProvider
    readonly property var usage: bridge.chatUsage || ({})
    readonly property var reported: usage.reported || ({})
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
    // A row is applied by the model id it RESOLVES to: the CLI's short alias does not always get that model
    // (live: "haiku" was answered by claude-sonnet-5, "claude-haiku-4-5-20251001" by Haiku).
    function claudeValue(m) { return m.id === "default" ? "" : (m.resolved || m.id) }
    function claudeEntry(id) { var key = id || "default"; return claudeModels.filter(function(m) { return m.id === key || (id && m.resolved === id) })[0] || null }
    function codexEntry(id) { return codexModels.filter(function(m) { return m.id === id })[0] || null }
    // The short display name; the canonical id never changes with it.
    function shortName(id, fallback) { return bridge.modelLabel(id || "", fallback || "") }
    // Claude's choice: drafted here, persisted the moment it is made.
    QtObject { id: claudeDraft; property string model: ""; property string effort: "" }
    function applyClaude(model, effort) { claudeDraft.model = model; claudeDraft.effort = effort; bridge.applyModel(model, effort) }

    component Caption: Text { color: Theme.textMuted; font { family: Theme.font; pixelSize: 10; letterSpacing: 2 } }
    component Line: Text { width: parent ? parent.width : 0; wrapMode: Text.Wrap; color: Theme.textLabel; font { family: Theme.font; pixelSize: 12 } }
    component Choice: Rectangle {
        id: choice
        property string title: ""
        property string meta: ""
        property bool selected: false
        property bool selectable: true
        signal chosen()
        width: parent ? parent.width : 0; height: 44; radius: Theme.radiusSmall
        opacity: selectable ? 1 : 0.55
        color: area.containsMouse && selectable ? Theme.alpha(Theme.commandEdge, 0.08) : selected ? Theme.alpha(Theme.commandEdge, 0.05) : "transparent"
        border.width: 1
        border.color: selected ? Theme.alpha(Theme.commandEdge, 0.42) : Theme.alpha(Theme.text, 0.05)
        MouseArea { id: area; anchors.fill: parent; hoverEnabled: true; enabled: choice.selectable
                    cursorShape: Qt.PointingHandCursor; onClicked: choice.chosen() }
        Column { x: 12; anchors.verticalCenter: parent.verticalCenter; width: parent.width - 80; spacing: 1
            Text { width: parent.width; elide: Text.ElideRight; text: choice.title; color: choice.selected ? Theme.commandEdge : Theme.text
                   font { family: Theme.font; pixelSize: 14 } }
            Text { width: parent.width; elide: Text.ElideRight; text: choice.meta; color: Theme.textDim; font { family: Theme.font; pixelSize: 11 } } }
        Text { visible: choice.selected; anchors.verticalCenter: parent.verticalCenter; x: parent.width - 24; text: "✓"; color: Theme.commandEdge
               font { family: Theme.font; pixelSize: 14; weight: Font.DemiBold } }
    }

    Flickable {
        anchors.fill: parent
        contentHeight: col.height + 8
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded; width: 6 }
        Column {
            id: col
            width: parent.width - 10; spacing: 10

            // -- 1. PROVIDER ----------------------------------------------------
            Caption { text: qsTr("PROVIDER") }
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

            // A provider that cannot be chosen can still be READ: its own read-only
            // details open here, and the active chat provider is untouched by it.
            Item {
                objectName: "providerDetails_google"
                width: parent.width; height: visible ? 22 : 0
                visible: googleDetails.groups.length > 0
                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    text: googleDetails.shown ? qsTr("Hide Antigravity quota") : qsTr("Show Antigravity quota (read-only)")
                    color: detailArea.containsMouse ? Theme.accent : Theme.textMuted
                    font { family: Theme.font; pixelSize: 11 }
                }
                MouseArea { id: detailArea; anchors.fill: parent; hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor; onClicked: googleDetails.shown = !googleDetails.shown }
            }

            // Google / Antigravity: read-only, on request, and only here. Each pool
            // is its own bar exactly as the CLI reported it - never merged.
            Column {
                id: googleDetails
                property bool shown: false
                readonly property var google: modelPanel.provider("google")
                readonly property var groups: google && google.quota ? google.quota.groups || [] : []
                visible: shown && groups.length > 0
                width: parent.width; spacing: 8
                Caption { text: qsTr("ANTIGRAVITY QUOTA (READ-ONLY)") }
                Repeater {
                    model: googleDetails.groups
                    Column {
                        id: pool
                        objectName: "googlePool_" + index
                        readonly property int pos: index
                        width: googleDetails.width; spacing: 4
                        readonly property var buckets: modelData.buckets || []
                        Text { objectName: "googlePoolName_" + pool.pos; text: modelData.name; color: Theme.textLabel
                               font { family: Theme.font; pixelSize: 12; weight: Font.DemiBold } }
                        Repeater {
                            model: pool.buckets
                            QuotaBar {
                                objectName: "googleQuota_" + pool.pos + "_" + modelData.window
                                tone: Theme.materialBlue
                                label: modelData.window === "weekly" ? qsTr("Weekly") : qsTr("5-hour")
                                remaining: Math.round(modelData.remaining_fraction * 100)
                                resetText: new Date(modelData.reset_time).toLocaleString(Qt.locale(i18n.qtLocale), Locale.ShortFormat)
                            }
                        }
                    }
                }
            }
            Rectangle { width: parent.width; height: 1; color: Theme.border }

            // -- Claude: 2. MODEL, 3. EFFORT, 4. USAGE / QUOTA --------------------
            Column {
                visible: modelPanel.chosen === "claude"
                width: parent.width; spacing: 10
                Caption { text: qsTr("MODEL") }
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
                        onChosen: { modelField.text = ""; modelPanel.applyClaude("", claudeDraft.effort); claudeField.open = false }
                    }
                    Repeater {
                        model: modelPanel.claudeModels.filter(function(m) { return m.id !== "default" })
                        Choice {
                            objectName: "claudeModel_" + modelData.id
                            title: modelPanel.shortName(modelPanel.claudeValue(modelData), modelData.name)
                            meta: modelPanel.claudeValue(modelData)
                            selected: claudeDraft.model === modelPanel.claudeValue(modelData) || claudeDraft.model === modelData.id
                            onChosen: {
                                modelField.text = ""
                                var keep = claudeDraft.effort && modelData.efforts.indexOf(claudeDraft.effort) >= 0 ? claudeDraft.effort : ""
                                modelPanel.applyClaude(modelPanel.claudeValue(modelData), keep)
                                claudeField.open = false
                            }
                        }
                    }
                }
                Line { visible: modelPanel.claude && modelPanel.claude.status === "available" && modelPanel.claudeModels.length === 0
                       text: qsTr("The CLI did not list its models; enter one below.") }
                Text { text: qsTr("Other model (as the CLI names it)"); color: Theme.textMuted; font { family: Theme.font; pixelSize: 11 } }
                TextField {
                    id: modelField; objectName: "modelField"
                    width: parent.width; height: 36; leftPadding: 12; rightPadding: 12
                    placeholderText: claudeDraft.model && !modelPanel.claudeEntry(claudeDraft.model) ? claudeDraft.model : qsTr("e.g. claude-sonnet-5")
                    placeholderTextColor: Theme.textDim; color: Theme.text
                    selectByMouse: true; font { family: Theme.font; pixelSize: 13 }
                    // A typed id is applied when it is FINISHED (Enter, or leaving the
                    // field), never per keystroke: half an id is not a model choice.
                    onEditingFinished: if (text.trim() && text.trim() !== claudeDraft.model) modelPanel.applyClaude(text.trim(), claudeDraft.effort)
                    background: Rectangle { radius: 6; color: Theme.surfaceDeep; border.width: 1; border.color: modelField.activeFocus ? Theme.commandEdge : Theme.border }
                }
                Caption { text: qsTr("EFFORT") }
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
                Caption { text: qsTr("USAGE / QUOTA") }
                Line { objectName: "configuredModelLine"
                       text: qsTr("Configured model: %1 · effort: %2").arg(bridge.config.claude_model || qsTr("CLI default")).arg(bridge.config.claude_effort || qsTr("Default")) }
                Line { objectName: "actualModelLine"
                       text: modelPanel.usage.provider === "anthropic" && modelPanel.reported.model ? qsTr("Answered the last call: %1").arg(modelPanel.reported.model)
                             : qsTr("Answered the last call: no Claude call yet") }
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
                Line { objectName: "quotaLine"
                       text: Object.keys(bridge.claudeLimits || {}).length === 0
                             ? qsTr("Plan limits: shown after the first Claude call - Claude Code reports them with each answer, and JARVIS makes no call just to check.")
                             : qsTr("As reported by Claude Code at %1 · extra paid usage: %2").arg(modelPanel.when(bridge.claudeLimits.seen_at))
                                   .arg(bridge.claudeLimits.using_overage === true ? qsTr("IN USE") : bridge.claudeLimits.overage_status === "rejected" ? qsTr("off") : qsTr("not reported")) }
                Line { objectName: "usageLine"
                       text: modelPanel.usage.provider !== "anthropic" || Object.keys(modelPanel.reported).length === 0 ? qsTr("Last call: no usage reported yet")
                             : (modelPanel.usage.call_count > 1 ? qsTr("Last turn: %1 calls, one per group. Tokens summed over all of them:").arg(modelPanel.usage.call_count) + " " : "")
                               + qsTr("Last call tokens: %1 input · %2 cache read · %3 cache write · %4 output")
                                 .arg(modelPanel.tokens(modelPanel.reported.input_tokens))
                                 .arg(modelPanel.tokens(modelPanel.reported.cache_read_input_tokens))
                                 .arg(modelPanel.tokens(modelPanel.reported.cache_creation_input_tokens))
                                 .arg(modelPanel.tokens(modelPanel.reported.output_tokens)) }
                Line { text: qsTr("Web Research: available on this provider (read-only public web search).") }
                Line { objectName: "claudeAttachmentLine"; text: qsTr("Attachments: images and text-like files, exactly the ones you pick.") }
            }

            // -- Codex: 2. MODEL, 3. EFFORT, 4. USAGE / QUOTA ---------------------
            Column {
                id: codexBlock
                visible: modelPanel.chosen === "codex"
                width: parent.width; spacing: 10
                readonly property var current: modelPanel.codexEntry(bridge.codexModel)
                Caption { text: qsTr("MODEL") }
                Collapsed {
                    id: codexField
                    objectName: "codexModelField"
                    title: bridge.codexModel ? modelPanel.shortName(bridge.codexModel, bridge.codexModel) : qsTr("No model chosen")
                    meta: bridge.codexModel ? bridge.codexModel : qsTr("Choose one of the models Codex lists")
                    Repeater {
                        model: modelPanel.codexModels
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
                Line { visible: !!modelPanel.codex && modelPanel.codex.status === "available" && modelPanel.codexModels.length === 0
                       text: qsTr("Codex listed no model that JARVIS can run with every tool disabled.") }
                Line { visible: !!bridge.codexModel && !codexBlock.current && modelPanel.codexModels.length > 0
                       text: qsTr("The saved model %1 is not offered now; calls are refused until you choose one.").arg(bridge.codexModel) }
                Caption { text: qsTr("EFFORT") }
                Segmented {
                    objectName: "codexEffort"
                    enabled: !!codexBlock.current
                    value: bridge.codexEffort
                    onPicked: function(v) { bridge.setCodexModel(bridge.codexModel, v) }
                    options: (codexBlock.current ? codexBlock.current.efforts : []).map(function(e) { return { value: e, label: e } })
                }
                Rectangle { width: parent.width; height: 1; color: Theme.border }
                Caption { text: qsTr("USAGE / QUOTA") }
                Line { text: qsTr("Configured model: %1 · effort: %2").arg(bridge.codexModel || qsTr("none")).arg(bridge.codexEffort || qsTr("model default")) }
                Line { objectName: "codexActualModelLine"
                       text: qsTr("Answering model: Codex does not report it - the call requests %1").arg(bridge.codexModel || qsTr("none")) }
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
                Line { text: Object.keys(bridge.codexQuota || {}).length === 0 ? qsTr("Plan limits: not reported yet.")
                             : qsTr("As reported by Codex at %1 · purchased credits: %2").arg(modelPanel.when(bridge.codexQuota.seen_at))
                                   .arg(bridge.codexQuota.has_credits === true ? qsTr("present") : bridge.codexQuota.has_credits === false ? qsTr("none") : qsTr("not reported")) }
                Line { text: modelPanel.usage.provider !== "openai" || Object.keys(modelPanel.reported).length === 0 ? qsTr("Last call: no usage reported yet")
                             : (modelPanel.usage.call_count > 1 ? qsTr("Last turn: %1 calls, one per group. Tokens summed over all of them:").arg(modelPanel.usage.call_count) + " " : "")
                               + qsTr("Last call tokens: %1 input · %2 cached · %3 output · %4 reasoning")
                                 .arg(modelPanel.tokens(modelPanel.reported.input_tokens)).arg(modelPanel.tokens(modelPanel.reported.cached_input_tokens))
                                 .arg(modelPanel.tokens(modelPanel.reported.output_tokens)).arg(modelPanel.tokens(modelPanel.reported.reasoning_output_tokens)) }
                Line { text: qsTr("Web Research: not available with Codex in this build - research questions are refused, never sent to another provider.") }
                Line { objectName: "codexAttachmentLine"; text: qsTr("Attachments: images and text-like files, exactly the ones you pick.") }
            }
        }
    }
}
