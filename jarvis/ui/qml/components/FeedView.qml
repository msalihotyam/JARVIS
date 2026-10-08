import QtQuick
import "../theme"

// The conversation stream. JARVIS answers sit on the LEFT: the luminous ring
// avatar, the name, the answer on its quieter glass surface, the time
// beneath. The operator's turns sit on the RIGHT in bounded deep-navy
// technical bubbles, the time beneath. System, error, unknown and approval
// entries stay on the left in their own colour. Long content stays readable
// and selectable; nothing is invented for the stream.
//
// Scrolling is a Flickable over one Column: every entry is laid out, so the
// content height, the thumb size and the thumb travel are exact (a ListView
// estimates them from the few delegates it has and drifts). Reading
// position is the reader's: following the end is what happens while they
// are at the end; once they scroll up to read older messages, a new entry,
// a composer resize or any relayout leaves them exactly where they are.
//
// The scrollbar is transient: invisible at rest, shown at once on any
// scroll, 2 px while scrolling and 3 px under the pointer or in a drag, held
// one second after the last interaction and faded in 200 ms. Its 16 px
// interaction strip is always reserved, so messages never shift when it
// appears; no overflow, no scrollbar.
Item {
    id: feed
    property bool referenceLayout: false
    // The conversation-integrated safety cards, after the last entry (MAIN / DARK).
    property bool safetyCards: false
    // The avatar column scales with the surface (1.0 at the 1920x1080 grid).
    property real layoutScale: 1
    readonly property int avatarSize: referenceLayout ? 36 : 50
    readonly property int messageX: referenceLayout ? avatarSize + 12 : 68
    readonly property int strip: 16
    // For inspection: the scroll state.
    readonly property alias contentY: flick.contentY
    readonly property alias contentHeight: flick.contentHeight
    readonly property alias viewportHeight: flick.height
    property alias followEnd: flick.followEnd
    readonly property bool overflow: flick.contentHeight > flick.height + 1
    function scrollTo(y) { glide.stop(); flick.contentY = Math.max(0, Math.min(flick.endY, y)) }
    // For inspection: how many glides have started, and whether one is running.
    readonly property alias gliding: glide.running
    property int glideCount: 0
    // A newly shown approval card glides into view when the reader is at the
    // end. Reading older messages, the position is kept and the jump-to-latest
    // arrow (plus the attention strip) leads to it - nothing is lost.
    readonly property bool approvalCardShown: safetyLoader.item ? safetyLoader.item.approvalShown : false
    // Its arrival also resizes the viewport (the attention strip appears), so
    // that resize joins the same single glide instead of jumping.
    property bool approvalGlide: false
    onApprovalCardShownChanged: if (approvalCardShown) { if (flick.followEnd) { approvalGlide = true; growth.restart() } else newBelow = true }
    // Newer content arrived below while the reader was reading older messages.
    property bool newBelow: false
    readonly property bool jumpVisible: newBelow && !flick.followEnd
    // The arrow and sending a message: one smooth glide to the exact end.
    function jumpToLatest() {
        newBelow = false; flick.followEnd = true
        if (Math.abs(flick.endY - flick.contentY) > 1) {
            glideCount += 1
            glide.stop(); glide.from = flick.contentY; glide.to = flick.endY; glide.start()
        }
    }
    Connections { target: bridge; function onInputAccepted() { feed.jumpToLatest() } }
    // Review: bring the card for an attention key into view and move focus to
    // its heading (never to a decision button). "decision" answers happen in
    // the choices / composer below the stream, so the end is shown.
    function reveal(key) {
        var heading = safetyLoader.item ? safetyLoader.item.headingFor(key) : null
        if (heading && heading.visible) {
            var card = heading.parent.parent
            scrollTo(safetyLoader.y + card.y - 12)
            flick.followEnd = flick.contentY >= flick.endY - 6
            heading.forceActiveFocus()
        } else {
            scrollTo(flick.endY); flick.followEnd = true
        }
    }

    // One compact, visually secondary message action.
    component MessageAction: Text {
        property string label: ""
        signal activated()
        text: label
        renderType: Theme.textRender
        color: actionArea.containsMouse ? Theme.accent : Theme.textMuted
        font: Theme.chatMeta
        Accessible.role: Accessible.Button
        Accessible.name: label
        MouseArea { id: actionArea; anchors.fill: parent; anchors.margins: -5; hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor; onClicked: parent.activated() }
    }

    // -- Markdown: presented, never executed, and never the source of truth ----
    // The canonical assistant text (modelData.text) is what history, Copy and
    // quoting use. What is SHOWN is this: the same text with & and < escaped, so
    // no HTML, script, image or embedded object can arrive from a provider, and
    // with single newlines turned into Markdown hard breaks, so a reply that
    // separates its lines with one newline still reads as separate lines. A
    // fenced code block is passed through untouched.
    function displayMarkdown(raw) {
        var safe = String(raw).replace(/&/g, "&amp;").replace(/</g, "&lt;")
        var parts = safe.split(/(```[\s\S]*?```|```[\s\S]*$)/)
        for (var i = 0; i < parts.length; i++) {
            if (i % 2 === 1) continue
            // A continuation line stays IN its paragraph: a line separator breaks
            // the line without opening a block, so it keeps the reading line
            // height instead of taking the gap that separates paragraphs.
            parts[i] = parts[i].replace(/([^\n])\n(?![\n]|[ \t]*(?:[-*+>|#]|\d+[.)]|```|---))/g, "$1 ")
            // Before a list, a heading, a quote, a table or a fence the newline is
            // real Markdown structure, so there it stays a hard break.
            parts[i] = parts[i].replace(/([^\n])\n(?!\n)/g, "$1  \n")
        }
        return parts.join("")
    }

    // -- response telemetry: measured, never estimated ----------------------------
    // Seconds with one decimal below a minute, whole minutes and seconds above;
    // tokens as the provider reported them. "Thinking" is only ever a provider's
    // own reasoning time; otherwise the second figure is the first visible text.
    function duration(ms, locale) {
        if (ms < 60000) return qsTr("%1 s").arg(Number(ms / 1000).toLocaleString(Qt.locale(locale), "f", 1))
        var total = Math.round(ms / 1000)
        return qsTr("%1 min %2 s").arg(Math.floor(total / 60)).arg(total % 60)
    }
    function telemetryLine(t, locale) {
        if (!t || t.elapsed_ms === undefined) return ""
        var parts = [duration(t.elapsed_ms, locale)]
        if (t.thinking_ms !== undefined) parts.push(qsTr("Thinking %1").arg(duration(t.thinking_ms, locale)))
        else if (t.first_ms !== undefined) parts.push(qsTr("First response %1").arg(duration(t.first_ms, locale)))
        if (t.tokens !== undefined) {
            var tokenText = t.tokens < 1000 ? qsTr("%1 tokens").arg(t.tokens)
                                            : qsTr("%1K tokens").arg(Number(t.tokens / 1000).toLocaleString(Qt.locale(locale), "f", 1))
            // A total that is mostly cheap cache reads must never read as an
            // equally expensive fresh call: named beside it, both exactly as
            // reported (never estimated).
            if (t.cached_tokens)
                tokenText = qsTr("%1 (%2 cached)").arg(tokenText).arg(
                    t.cached_tokens < 1000 ? String(t.cached_tokens)
                                           : Number(t.cached_tokens / 1000).toLocaleString(Qt.locale(locale), "f", 1) + "K")
            parts.push(tokenText)
        }
        return parts.join(" · ")
    }

    function railColor(kind) {
        switch (kind) {
        case "user": return Theme.accentSoft
        case "jarvis": return Theme.accent
        case "result": return Theme.accentSoft
        case "error": return Theme.danger
        case "unknown": return Theme.unknownEdge
        case "approval": return Theme.warning
        case "clarify": return Theme.accentSoft
        case "scheduled": return Theme.success
        case "data": return Theme.textMuted
        case "notice": return Theme.accentSoft
        default: return Theme.textDim
        }
    }
    function kindLabel(kind) {
        switch (kind) {
        case "user": return qsTr("You")
        case "jarvis": return "JARVIS"
        // A task's own deliverable, never JARVIS's conversational voice.
        case "result": return qsTr("TASK RESULT")
        case "error": return qsTr("ERROR")
        case "unknown": return qsTr("OUTCOME UNKNOWN")
        case "approval": return qsTr("APPROVAL")
        case "clarify": return qsTr("JARVIS · needs one detail")
        case "scheduled": return qsTr("SCHEDULED")
        case "data": return qsTr("UNTRUSTED DATA")
        case "notice": return qsTr("NOTE")
        default: return qsTr("SYSTEM")
        }
    }

    Flickable {
        id: flick
        anchors.fill: parent
        clip: true
        contentWidth: width
        contentHeight: stack.height
        flickableDirection: Flickable.VerticalFlick
        boundsBehavior: Flickable.StopAtBounds
        maximumFlickVelocity: 2600
        readonly property real endY: Math.max(0, contentHeight - height)
        // True while the reader is at the end; updated from every change the
        // reader makes (wheel, flick, drag, thumb), never from our own settling.
        property bool followEnd: true
        property bool settling: false
        // Set after the first layout: only content that arrives later glides.
        property bool primed: false
        function remember() { followEnd = contentY >= endY - 6 }
        onContentYChanged: if (!settling && !glide.running) remember()
        // Content grew or the viewport changed: keep the end if the reader
        // was there, otherwise leave the reading position exactly as it is
        // (only pulling back inside the range when the range shrank). New
        // content glides to the end - short, eased, no bounce - and a glide
        // already under way is simply re-aimed at the new end.
        NumberAnimation { id: glide; target: flick; property: "contentY"; duration: Theme.dur(220); easing.type: Easing.OutCubic }
        function settle(smooth) {
            settling = true
            if (followEnd) {
                if (smooth && primed && Math.abs(endY - contentY) > 1) {
                    if (!glide.running) feed.glideCount += 1
                    glide.stop(); glide.from = contentY; glide.to = endY; glide.start()
                } else if (!glide.running) {
                    contentY = endY
                }
            } else if (contentY > endY) {
                contentY = endY
            }
            settling = false
        }
        function settleGrowth() {
            if (!followEnd && endY - contentY > 6) feed.newBelow = true
            settle(true); feed.approvalGlide = false
        }
        onFollowEndChanged: if (followEnd) feed.newBelow = false
        function settleResize() { primed = true; if (feed.approvalGlide) growth.restart(); else settle(false) }
        // A burst of growth (a card laying itself out in steps) is one glide:
        // the changes are gathered for a few frames, then settled once.
        Timer { id: growth; interval: 50; onTriggered: flick.settleGrowth() }
        onContentHeightChanged: growth.restart()
        onHeightChanged: if (feed.approvalGlide) growth.restart(); else Qt.callLater(settleResize)
        Component.onCompleted: Qt.callLater(settleResize)
        // The reader takes over at once.
        onMovementStarted: glide.stop()

        Column {
            id: stack
            width: flick.width - feed.strip
            spacing: feed.referenceLayout ? 18 : 16
            Repeater {
                model: bridge.feed
                delegate: Item {
                    id: row
                    objectName: "feedRow_" + kind
                    width: stack.width
                    height: Math.max(row.isUser ? 0 : avatar.height, card.height + stamp.height + stamp.gap)
                    readonly property string kind: modelData.kind
                    readonly property bool isUser: kind === "user"
                    readonly property bool isJarvis: kind === "jarvis" || kind === "clarify"
                    // What an approved task produced (a plan, an analysis, an account
                    // of what ran): shown, labelled with where it came from, and never
                    // presented as something JARVIS said.
                    readonly property bool isResult: kind === "result"
                    readonly property bool loud: kind === "error" || kind === "unknown" || kind === "approval"
                    // Provider prose may carry Markdown; JARVIS's own notices and
                    // untrusted external data never do.
                    readonly property bool markdown: modelData.chat === true || isResult
                    readonly property color tone: feed.railColor(kind)
                    // Appearance: fade + short slide, no bounce. A reply that streamed is
                    // already on screen: its final entry takes over without a second entrance.
                    opacity: modelData.streamed === true ? 1 : 0
                    transform: Translate { id: slide; y: modelData.streamed === true ? 0 : Theme.travel }
                    Component.onCompleted: if (modelData.streamed !== true) appear.start()
                    ParallelAnimation {
                        id: appear
                        NumberAnimation { target: row; property: "opacity"; to: 1; duration: Theme.panelIn; easing.type: Easing.OutCubic }
                        NumberAnimation { target: slide; property: "y"; to: 0; duration: Theme.panelIn; easing.type: Easing.OutCubic }
                    }
                    // -- avatar column (JARVIS and the state markers; not the operator) --
                    Item {
                        id: avatar
                        visible: !row.isUser
                        width: feed.avatarSize; height: width
                        readonly property real k: width / 60
                        // JARVIS: the simplified Reactor-derived identity mark - a dark
                        // circular housing, one controlled blue annulus with three
                        // subdued radial cuts, a dark optical centre. 32 px, minimal
                        // bloom, inside a 36 px reserve so nothing clips.
                        Item {
                            objectName: row.isJarvis ? "feedAvatar_jarvis" : ""
                            visible: row.isJarvis
                            anchors.centerIn: parent; width: 32; height: 32
                            Rectangle { anchors.centerIn: parent; width: 36; height: 36; radius: 18; color: Theme.alpha("#36aaff", 0.09) }
                            Rectangle { anchors.fill: parent; radius: 16; color: "#061424"; border.width: 1; border.color: "#2a5f8f" }
                            Rectangle { anchors.centerIn: parent; width: 22; height: 22; radius: 11; color: "transparent"; border.width: 4; border.color: "#3aa8f0" }
                            Repeater { model: 3
                                Rectangle { width: 1.5; height: 8; color: "#061424"; x: 16 - 0.75; y: 16 - 11.5
                                    transform: Rotation { origin.x: 0.75; origin.y: 11.5; angle: 90 + index * 120 } } }
                            Rectangle { anchors.centerIn: parent; width: 12; height: 12; radius: 6; color: "#03101c" }
                        }
                        // Everything else: a state marker on the rail.
                        Rectangle {
                            visible: !row.isJarvis
                            anchors.centerIn: parent; width: 30; height: 30; radius: 15
                            color: Theme.alpha(row.tone, 0.12); border.width: 1; border.color: Theme.alpha(row.tone, 0.5)
                            Rectangle { anchors.centerIn: parent; width: 8; height: 8; radius: 4; color: row.tone }
                        }
                    }

                    // The operator's bubble is content-sized: measured unwrapped here,
                    // bounded to the right part of the stream.
                    Text { id: probe; visible: false; text: modelData.text; font: message.font }

                    // -- body --------------------------------------------------------------
                    Rectangle {
                        id: card
                        objectName: "feedCard_" + row.kind
                        readonly property real pad: feed.referenceLayout ? 18 : 20
                        readonly property real maxUser: Math.min(720, Math.max(280, row.width * 0.62))
                        x: row.isUser ? row.width - width : feed.messageX
                        width: row.isUser ? Math.min(maxUser, Math.ceil(probe.implicitWidth) + 2 * pad + 2)
                             : row.loud ? Math.min(row.width - x, Math.max(360, row.width * 0.694))
                             : row.width - x
                        readonly property real textWidth: (row.isUser || row.loud) ? width - 2 * pad : width
                        height: body.height + (row.isUser ? 2 * (feed.referenceLayout ? 13 : 14) : row.loud ? (feed.referenceLayout ? 31 : 34) : 4)
                        radius: 12
                        // The request: a compact, clearly bounded deep-navy technical
                        // block with a thin cool-blue perimeter and a top highlight.
                        color: row.loud ? Theme.glassPrimaryReading : "transparent"
                        gradient: row.isUser ? requestGlass : null
                        Gradient { id: requestGlass
                            GradientStop { position: 0; color: Theme.glassUserReading }
                            GradientStop { position: 1; color: Theme.glassPrimaryReading } }
                        border.width: row.isUser || row.loud ? 1 : 0
                        border.color: row.isUser ? "#b8327bb5" : Theme.alpha(row.tone, 0.45)
                        Rectangle { visible: row.isUser; x: 14; y: 1; width: parent.width - 28; height: 1; color: "#5a6fd2f5" }
                        Rectangle { visible: row.isUser; anchors { left: parent.left; right: parent.right; top: parent.top; margins: 1 } height: Math.min(parent.height * 0.45, 34); radius: 11
                            gradient: Gradient { GradientStop { position: 0; color: "#0f4fc3ff" } GradientStop { position: 1; color: "transparent" } } }
                        Column {
                            id: body
                            x: row.isUser || row.loud ? card.pad : 0
                            y: row.isUser ? (feed.referenceLayout ? 13 : 14) : row.loud ? (feed.referenceLayout ? 15 : 16) : 2
                            width: card.textWidth
                            spacing: row.isJarvis ? 0 : (feed.referenceLayout ? 7 : 9)
                            // The label line: JARVIS carries its name inside its surface;
                            // the operator's bubble needs none; the rest are labelled.
                            Text { visible: row.loud || row.isResult || row.kind === "system" || row.kind === "scheduled" || row.kind === "data"
                                   text: feed.kindLabel(row.kind)
                                         + (row.isResult && modelData.capability ? "  ·  " + modelData.capability : "")
                                         + (row.isResult && modelData.destination ? "  ·  " + modelData.destination : "")
                                   renderType: Theme.textRender
                                   color: row.tone
                                   font: feed.referenceLayout ? Theme.tracked(Theme.messageIdentity, 0.72) : Theme.label }
                            Item {
                                width: parent.width
                                height: (sent.visible ? sent.y + sent.height : sources.visible ? sources.y + sources.height : message.y + message.height) + (row.isJarvis ? 4 : 0)
                                // An ordinary JARVIS reply keeps its open shape; the only
                                // addition is the reading veil: dark close to the text and
                                // feathered outward (a nine-slice sheet, no effect), so the
                                // planet never competes with the answer and no card
                                // silhouette appears. No border, no tail, no bubble.
                                BorderImage {
                                    objectName: "readingSurface"
                                    visible: (row.isJarvis || row.isResult) && !row.loud
                                    z: -1
                                    readonly property int feather: 36          // the sheet's falloff margin
                                    readonly property int slice: 46            // feather + the core's corner radius
                                    source: "../assets/reading-veil.png"
                                    // Fully covered: the answer plus 10 px each side and 8 px above / below.
                                    x: -10 - feather
                                    y: message.y - 8 - feather
                                    width: Math.min(parent.width + 10, message.width + 20) + 2 * feather
                                    height: (sources.visible ? sources.y + sources.height : message.y + message.height) - message.y + 16 + 2 * feather
                                    border { left: slice; top: slice; right: slice; bottom: slice }
                                    opacity: Theme.glassReadingVeil
                                    smooth: true
                                }
                                // JARVIS: no surface at all - the small name, then the answer
                                // itself at a readable measure; the avatar is the identity.
                                Text { id: name; visible: row.isJarvis; x: 0; y: 5
                                       renderType: Theme.textRender
                                       objectName: modelData.interrupted === true ? "feedInterrupted" : ""
                                       // A reply that stopped part-way says so; it is never presented as an answer.
                                       text: feed.kindLabel(row.kind) + (modelData.interrupted === true ? "  ·  " + qsTr("INTERRUPTED - INCOMPLETE ANSWER") : "")
                                       color: modelData.interrupted === true ? Theme.warning : Theme.materialBlue
                                       font: Theme.chatAuthor
                                       lineHeightMode: Text.FixedHeight; lineHeight: 18 }
                                TextEdit {
                                    id: message
                                    x: 0
                                    y: row.isJarvis ? name.y + name.height + 4 : 0
                                    width: row.isJarvis ? Math.min(parent.width, 760) : parent.width
                                    // Canonical text in, presented text out: the raw
                                    // assistant text is never rewritten for display.
                                    text: row.markdown ? feed.displayMarkdown(modelData.text) : modelData.text
                                    textFormat: row.markdown ? TextEdit.MarkdownText : TextEdit.PlainText
                                    renderType: Theme.textRender
                                    readOnly: true
                                    selectByMouse: true
                                    // A selection survives the click on "Quote selection".
                                    persistentSelection: row.conversational
                                    wrapMode: Text.Wrap
                                    objectName: "feedMessage_" + row.kind
                                    color: row.kind === "system" ? Theme.textMuted : Theme.text
                                    selectionColor: Theme.accentSoft
                                    font: row.kind === "data" ? Theme.code
                                          : !feed.referenceLayout ? Theme.conversationCompact
                                          : row.isUser ? Theme.conversationUser : Theme.conversationBody
                                    // The reading rhythm (TextEdit has no lineHeight): 18/29 with a
                                    // 14 px paragraph gap for a reply, tighter for what the person
                                    // wrote, plus the heading, list and code scale of a Markdown
                                    // document. Applied to the TEXT, not just to the row: a delegate
                                    // is reused with another entry's text, and formatting that only
                                    // ran at creation would stay behind on the wrong words. The guard
                                    // is the formatting edit's own text-change notification.
                                    property bool formatting: false
                                    function applyRhythm() {
                                        if (formatting || !feed.referenceLayout || row.kind === "data" || typeof typography === "undefined")
                                            return
                                        formatting = true
                                        typography.setLineHeight(textDocument,
                                                                 row.isUser ? Theme.conversationUserLine : Theme.conversationLine,
                                                                 row.isUser ? 8 : Theme.conversationParagraph, row.markdown)
                                        formatting = false
                                    }
                                    Component.onCompleted: applyRhythm()
                                    onTextChanged: applyRhythm()
                                    // The compact layout has its own smaller type, so the reading
                                    // rhythm follows the layout a row is actually shown in.
                                    Connections { target: feed; function onReferenceLayoutChanged() { message.applyRhythm() } }
                                    // The offer to quote appears AT the selection, and only
                                    // for a message that may be quoted at all.
                                    onSelectedTextChanged: row.conversational ? feed.offerQuote(message, modelData.id) : feed.hideQuoteTip()
                                    // Right-click: Copy (the selection, or the whole message) and Select All.
                                    TapHandler { acceptedButtons: Qt.RightButton
                                                 onTapped: function(point) { messageMenu.editor = message; messageMenu.wholeText = modelData.text
                                                                             messageMenu.openAt(message, point.position) } }
                                }
                                // What the person attached to this message, still visible after
                                // sending: each file with the letter of the group that owned it,
                                // then the message's own files. Same cards as the draft.
                                Flow {
                                    id: sent
                                    objectName: "sentAttachments"
                                    readonly property var files: {
                                        var out = []
                                        var groups = modelData.groups || []
                                        for (var g = 0; g < groups.length; g++) {
                                            var owned = groups[g].attachments || []
                                            for (var i = 0; i < owned.length; i++) out.push({ file: owned[i], label: String.fromCharCode(65 + g) })
                                        }
                                        var own = modelData.attachments || []
                                        for (var j = 0; j < own.length; j++) out.push({ file: own[j], label: "" })
                                        return out
                                    }
                                    // One gutter for the ownership letters: reserved for every card
                                    // once any file belongs to a group, so the cards line up.
                                    readonly property bool lettered: files.some(function(f) { return f.label !== "" })
                                    visible: row.isUser && files.length > 0
                                    y: message.y + message.height + 8
                                    width: message.width
                                    spacing: 6
                                    Repeater {
                                        model: sent.files
                                        Item {
                                            width: card.width + (sent.lettered ? 26 : 0); height: card.height
                                            Rectangle { id: badge; visible: modelData.label !== ""; x: 0; y: (parent.height - 20) / 2; width: 20; height: 20; radius: 10
                                                        color: Theme.alpha("#36aaff", 0.2); border.width: 1; border.color: Theme.alpha("#49bdfa", 0.6)
                                                        Text { anchors.centerIn: parent; text: modelData.label; color: Theme.text
                                                               font: Theme.captionStrong } }
                                            AttachmentCard { id: card; x: sent.lettered ? 26 : 0; file: modelData.file; compact: true; nested: true
                                                             onOpened: bridge.previewAttachment(modelData.file) }
                                        }
                                    }
                                }
                                // Web Research: the public pages behind the answer. Untrusted
                                // links, plain text; a click opens only an http(s) page in the
                                // person's own browser - JARVIS itself visits nothing here.
                                Column {
                                    id: sources
                                    objectName: "feedSources"
                                    visible: (modelData.sources || []).length > 0
                                    y: message.y + message.height + 14
                                    width: message.width
                                    spacing: 5
                                    Text { objectName: "feedSourcesHeading"
                                           text: modelData.sources_cited ? qsTr("Sources") : qsTr("Search results (the answer names no specific page)")
                                           renderType: Theme.textRender
                                           color: Theme.textLabel; font: Theme.chatSourcesHeading }
                                    Repeater {
                                        model: modelData.sources || []
                                        delegate: Text {
                                            objectName: "feedSource_" + index
                                            width: sources.width
                                            elide: Text.ElideRight
                                            textFormat: Text.PlainText
                                            text: (index + 1) + ". " + (modelData.title || modelData.url) + "  ·  " + modelData.url.replace(/^https?:\/\//, "").split("/")[0]
                                            renderType: Theme.textRender
                                            color: linkArea.containsMouse ? Theme.accent : Theme.materialBlue
                                            font: Theme.chatSource
                                            MouseArea { id: linkArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                                                        onClicked: if (/^https?:\/\//.test(modelData.url)) Qt.openUrlExternally(modelData.url) }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    // The time, beneath the turn: at the right under the operator's
                    // bubble, at the left under JARVIS; a soft outline keeps it
                    // legible over the planet's bright horizon.
                    Text {
                        id: stamp
                        objectName: "feedStamp"
                        readonly property int gap: 5
                        x: row.isUser ? row.width - width - 6 : card.x + (row.isJarvis ? 18 : 0)
                        y: card.height + gap
                        text: modelData.time; color: Theme.textMuted
                        renderType: Theme.textRender
                        font: Theme.chatMeta
                        lineHeightMode: Text.FixedHeight; lineHeight: 18
                    }
                    // What a finished answer or task result took: beside its time, quieter
                    // still, and never on the person's own message.
                    Text {
                        id: telemetry
                        objectName: "feedTelemetry"
                        readonly property string line: row.isUser ? "" : feed.telemetryLine(modelData.telemetry, typeof i18n !== "undefined" ? i18n.qtLocale : "en_US")
                        visible: line !== ""
                        x: stamp.x + stamp.width + 14; y: stamp.y
                        text: line
                        color: Theme.textMuted
                        renderType: Theme.textRender
                        font: stamp.font
                        lineHeightMode: Text.FixedHeight; lineHeight: 18
                        Text { anchors.right: parent.left; anchors.rightMargin: 5; text: "·"; color: parent.color
                               renderType: Theme.textRender; font: parent.font; lineHeightMode: Text.FixedHeight; lineHeight: 18 }
                    }
                    // Copy / Quote, hidden until hover: only on what the person wrote
                    // and on General Chat replies - never on cards, notices, errors,
                    // external data or task results.
                    readonly property bool conversational: row.isUser || modelData.chat === true
                    HoverHandler { id: rowHover; enabled: row.conversational }
                    Row {
                        objectName: "messageActions_" + index
                        visible: row.conversational
                        opacity: rowHover.hovered || message.selectedText.length > 0 ? 1 : 0
                        Behavior on opacity { NumberAnimation { duration: Theme.hoverMs } }
                        spacing: 14
                        y: stamp.y
                        x: row.isUser ? stamp.x - width - 16 : (telemetry.visible ? telemetry.x + telemetry.width : stamp.x + stamp.width) + 16
                        MessageAction { objectName: "messageCopy_" + index; label: qsTr("Copy"); onActivated: bridge.copyText(modelData.text) }
                        // Quoting adds a REFERENCE to the draft - it never writes into
                        // the message being composed, and it authorizes nothing.
                        MessageAction { objectName: "messageQuote_" + index; label: qsTr("Quote"); onActivated: bridge.quoteMessage(modelData.id) }
                        MessageAction { objectName: "messageQuoteSelection_" + index; label: qsTr("Quote selection")
                                        visible: message.selectedText.length > 0
                                        onActivated: { bridge.quoteSelection(modelData.id, message.selectedText); message.deselect(); feed.hideQuoteTip() } }
                    }
                }
            }
            // THE TURN BEING ANSWERED: one live row, from the moment a request starts
            // until its answer stands. It carries what JARVIS is really doing - the
            // state the bridge reports, never an invented one - a small activity mark
            // and the running time, all on ONE line that is updated in place; while a
            // General Chat reply streams, its text grows above that same line and the
            // time keeps running. It is not part of the feed, so a delta rebuilds
            // nothing; the final entry replaces it in the same frame, and the measured
            // duration then stays under the finished answer (its telemetry line).
            // Waiting for the person (an approval, an answer) is said here too, calm
            // and untimed: nothing is running, so no clock pretends otherwise.
            Item {
                id: streamRow
                objectName: "streamingReply"
                readonly property bool streaming: bridge.streamText !== ""
                // Work is running: the core says so, or reply text is arriving right now.
                readonly property bool working: bridge.coreState === "running" || streaming
                readonly property string phase: streaming ? "writing"
                                                : working ? (bridge.executing ? "executing" : "preparing")
                                                : (bridge.coreState === "awaiting_approval" || bridge.coreState === "awaiting_input") ? bridge.coreState : ""
                readonly property string label: phase === "writing" ? qsTr("Writing the answer")
                                              : phase === "executing" ? qsTr("Executing")
                                              : phase === "preparing" ? Theme.stateLabel("running")
                                              : Theme.stateLabel(phase)
                // The running time of THIS request: from the bridge's own start of work.
                property real elapsedMs: 0
                function tick() { elapsedMs = bridge.workStartedAt > 0 ? Math.max(0, Date.now() - bridge.workStartedAt) : 0 }
                onWorkingChanged: tick()
                Timer { interval: 100; repeat: true; running: streamRow.working && streamRow.visible; triggeredOnStart: true; onTriggered: streamRow.tick() }
                visible: phase !== ""
                width: stack.width
                height: visible ? Math.max(feed.avatarSize, liveStatus.y + liveStatus.height + 6) : 0
                Item {
                    width: feed.avatarSize; height: width
                    Item { anchors.centerIn: parent; width: 32; height: 32
                        Rectangle { anchors.centerIn: parent; width: 36; height: 36; radius: 18; color: Theme.alpha("#36aaff", 0.09) }
                        Rectangle { anchors.fill: parent; radius: 16; color: "#061424"; border.width: 1; border.color: "#2a5f8f" }
                        Rectangle { anchors.centerIn: parent; width: 22; height: 22; radius: 11; color: "transparent"; border.width: 4; border.color: "#3aa8f0" }
                        Rectangle { anchors.centerIn: parent; width: 12; height: 12; radius: 6; color: "#03101c" } }
                }
                Text { id: streamName; x: feed.messageX; y: 7; text: "JARVIS"; color: Theme.materialBlue
                       renderType: Theme.textRender
                       font: Theme.chatAuthor
                       lineHeightMode: Text.FixedHeight; lineHeight: 18 }
                TextEdit {
                    id: streamText
                    objectName: "streamingReplyText"
                    visible: streamRow.streaming
                    x: feed.messageX; y: streamName.y + streamName.height + 4
                    width: Math.min(streamRow.width - feed.messageX, 760)
                    // Partial Markdown is safe to show: an unfinished emphasis or an
                    // unclosed fence renders as text, and the layout does not jump when
                    // the final row replaces this one.
                    text: feed.displayMarkdown(bridge.streamText)
                    readOnly: true
                    wrapMode: Text.Wrap
                    textFormat: TextEdit.MarkdownText
                    renderType: Theme.textRender
                    color: Theme.text
                    font: feed.referenceLayout ? Theme.conversationBody : Theme.conversationCompact
                    // The same reading rhythm as the final reply, re-applied as text grows (guarded:
                    // the formatting edit itself reports a text change).
                    property bool formatting: false
                    onTextChanged: if (!formatting && feed.referenceLayout && typeof typography !== "undefined") {
                        formatting = true
                        typography.setLineHeight(textDocument, Theme.conversationLine, Theme.conversationParagraph, true)
                        formatting = false }
                }
                // The one live line: activity mark, what is happening, for how long.
                Row {
                    id: liveStatus
                    objectName: "liveStatus"
                    // Where the finished answer's time and duration will stand.
                    x: feed.messageX + 18
                    y: streamRow.streaming ? streamText.y + streamText.height + 8 : streamName.y + streamName.height + 6
                    height: 20
                    spacing: 9
                    Row {
                        id: activity
                        objectName: "liveActivity"
                        // For inspection: the mark moves only while work is really running.
                        readonly property bool animated: streamRow.working && Theme.ambient
                        anchors.verticalCenter: parent.verticalCenter
                        spacing: 4
                        Repeater {
                            model: streamRow.working ? 3 : 1
                            Rectangle {
                                width: 5; height: 5; radius: 2.5
                                anchors.verticalCenter: parent.verticalCenter
                                color: streamRow.working ? Theme.accent : Theme.stateColor(bridge.coreState)
                                opacity: activity.animated ? 0.28 : 0.9
                                SequentialAnimation on opacity {
                                    running: activity.animated && streamRow.visible
                                    loops: Animation.Infinite
                                    PauseAnimation { duration: Theme.dur(index * 160) }
                                    NumberAnimation { to: 1; duration: Theme.dur(320); easing.type: Easing.InOutSine }
                                    NumberAnimation { to: 0.28; duration: Theme.dur(420); easing.type: Easing.InOutSine }
                                    PauseAnimation { duration: Theme.dur((2 - index) * 160 + 120) }
                                }
                            }
                        }
                    }
                    Text {
                        objectName: "liveStatusLabel"
                        anchors.verticalCenter: parent.verticalCenter
                        text: streamRow.label
                        color: streamRow.working ? Theme.textLabel : Theme.stateColor(bridge.coreState)
                        renderType: Theme.textRender
                        font: Theme.chatStatus
                    }
                    Text {
                        objectName: "liveStatusTimer"
                        visible: streamRow.working
                        anchors.verticalCenter: parent.verticalCenter
                        text: "·  " + feed.duration(streamRow.elapsedMs, typeof i18n !== "undefined" ? i18n.qtLocale : "en_US")
                        color: Theme.textMuted
                        renderType: Theme.textRender
                        font: Theme.chatStatus
                    }
                }
                HoverHandler { id: streamHover }
                Row {
                    visible: streamRow.streaming
                    opacity: streamHover.hovered ? 1 : 0
                    spacing: 14
                    anchors.verticalCenter: liveStatus.verticalCenter
                    x: liveStatus.x + liveStatus.width + 18
                    MessageAction { objectName: "streamCopy"; label: qsTr("Copy"); onActivated: bridge.copyText(bridge.streamText) }
                }
            }
            Loader {
                id: safetyLoader
                width: stack.width
                active: feed.safetyCards
                // No visible binding on its own height: an invisible parent would
                // hide the cards and so collapse that height. Empty is zero-sized,
                // which the stack skips.
                sourceComponent: Component { SafetyCards { width: stack.width; inset: feed.messageX } }
            }
        }
    }

    // -- jump to latest: a small arrow just above the composer, only while newer
    //    content waits below the reading position ---------------------------------
    Rectangle {
        id: jump
        objectName: "jumpToLatest"
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.horizontalCenterOffset: -feed.strip / 2
        anchors.bottom: parent.bottom; anchors.bottomMargin: 12
        width: 36; height: 36; radius: 18
        opacity: feed.jumpVisible ? 1 : 0
        visible: opacity > 0
        Behavior on opacity { NumberAnimation { duration: Theme.dur(160); easing.type: Theme.ease } }
        color: jumpArea.containsMouse ? Theme.alpha(Theme.accent, 0.22) : Theme.glassPrimaryReading
        border.width: 1; border.color: feed.approvalCardShown ? Theme.warning : Theme.alpha(Theme.accent, 0.55)
        Accessible.role: Accessible.Button
        Accessible.name: qsTr("Jump to latest message")
        Icon { anchors.centerIn: parent; name: "arrow-down"; size: 16; tint: Theme.text }
        MouseArea { id: jumpArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                    enabled: feed.jumpVisible; onClicked: feed.jumpToLatest() }
    }

    // -- the transient scrollbar, in the reserved strip -------------------------
    Item {
        id: bar
        objectName: "feedScrollBar"
        x: feed.width - feed.strip; y: 0; width: feed.strip; height: feed.height
        visible: feed.overflow
        readonly property real thumbH: Math.max(24, Math.round(height * flick.height / Math.max(1, flick.contentHeight)))
        readonly property real travel: Math.max(0, height - thumbH)
        readonly property bool engaged: hit.pressed || hit.containsMouse
        property bool recent: false
        readonly property bool shown: engaged || flick.moving || recent
        Timer { id: hold; interval: 1000; onTriggered: bar.recent = false }
        function poke() { recent = true; hold.restart() }
        // Any scroll the reader makes shows the bar; when the pointer leaves or
        // the drag ends, hold, then fade. The fade runs both ways (K2: an exit
        // only where there is an entry).
        Connections { target: flick; function onContentYChanged() { if (!flick.settling) bar.poke() } }
        onEngagedChanged: if (!engaged) poke()
        opacity: shown ? 1 : 0
        Behavior on opacity { NumberAnimation { duration: Theme.hoverMs; easing.type: Theme.ease } }
        Rectangle {
            id: thumb
            objectName: "feedScrollThumb"
            anchors.right: parent.right; anchors.rightMargin: 6
            width: bar.engaged ? 3 : 2
            height: bar.thumbH
            y: flick.endY > 0 ? bar.travel * Math.min(1, Math.max(0, flick.contentY / flick.endY)) : 0
            radius: 1.5
            color: bar.engaged ? "#5cc0f0" : "#3a9fdb"
            Behavior on width { NumberAnimation { duration: Theme.hoverMs; easing.type: Theme.ease } }
        }
        MouseArea {
            id: hit
            anchors.fill: parent
            hoverEnabled: true
            preventStealing: true
            property real grab: 0
            function moveTo(py) {
                var t = Math.min(1, Math.max(0, (py - grab) / Math.max(1, bar.travel)))
                flick.contentY = t * flick.endY
            }
            onPressed: function(mouse) {
                var onThumb = mouse.y >= thumb.y && mouse.y <= thumb.y + thumb.height
                grab = onThumb ? mouse.y - thumb.y : thumb.height / 2
                if (!onThumb) moveTo(mouse.y)
            }
            onPositionChanged: function(mouse) { if (pressed) moveTo(mouse.y) }
        }
    }

    // One read-only menu for every message: the right-clicked one is its editor.
    EditMenu { id: messageMenu; objectName: "messageEditMenu" }

    // -- the contextual quote action ------------------------------------------
    // Selecting text inside a message JARVIS or the person wrote offers to quote
    // exactly that selection, right where the selection is. It follows the text
    // while the view scrolls, leaves as soon as the selection is cleared or the
    // conversation is rebuilt, and never interferes with selecting.
    function offerQuote(edit, entryId) {
        if (!edit || edit.selectedText.length === 0) { hideQuoteTip(); return }
        quoteTip.source = edit
        quoteTip.entryId = String(entryId || "")
        quoteTip.place()
    }
    function hideQuoteTip() { quoteTip.source = null; quoteTip.shown = false }
    Connections { target: bridge; function onFeedChanged() { feed.hideQuoteTip() } }

    Item {
        id: quoteTip
        objectName: "quoteTip"
        property var source: null
        property string entryId: ""
        property bool shown: false
        visible: shown
        z: 60
        width: tipLabel.width + 20; height: 26
        function place() {
            if (!source || !source.visible || source.selectedText.length === 0) { shown = false; return }
            var start = Math.min(source.selectionStart, source.selectionEnd)
            var box = source.positionToRectangle(start)
            var point = source.mapToItem(feed, box.x, box.y)
            x = Math.max(4, Math.min(point.x - 6, feed.width - width - feed.strip - 4))
            y = point.y - height - 6
            // Outside the visible part of the conversation it simply does not show.
            shown = y > 0 && y < feed.height - height
        }
        Connections { target: flick; function onContentYChanged() { quoteTip.place() } }
        Connections { target: feed; function onHeightChanged() { quoteTip.place() } }
        Rectangle {
            anchors.fill: parent; radius: 7
            color: tipArea.containsMouse ? Theme.alpha(Theme.accent, 0.22) : Theme.glassMenu
            border.width: 1; border.color: Theme.glassEdgeStrong
        }
        Text { id: tipLabel; anchors.centerIn: parent; text: qsTr("Quote selection"); color: Theme.text
               font: Theme.captionStrong }
        MouseArea {
            id: tipArea
            objectName: "quoteTipButton"
            anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
            onClicked: {
                if (quoteTip.source) {
                    bridge.quoteSelection(quoteTip.entryId, quoteTip.source.selectedText)
                    quoteTip.source.deselect()
                }
                feed.hideQuoteTip()
            }
        }
    }
}
