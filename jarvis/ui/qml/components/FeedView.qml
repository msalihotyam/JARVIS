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
            scrollTo(card.mapToItem(stack, 0, 0).y - 12)
            flick.followEnd = flick.contentY >= flick.endY - 6
            heading.forceActiveFocus()
        } else {
            scrollTo(flick.endY); flick.followEnd = true
        }
    }

    // One compact, visually secondary message action. With a confirmation it
    // says so briefly after it ran ("Copied"): a word that stays a moment, not
    // a motion, so it reads the same with ambient effects off.
    component MessageAction: Text {
        id: action
        property string label: ""
        property string confirmation: ""
        readonly property alias confirming: held.running
        signal activated()
        function trigger() { activated(); if (confirmation !== "") held.restart() }
        Timer { id: held; interval: 1500 }
        text: confirming ? confirmation : label
        renderType: Theme.textRender
        color: confirming ? Theme.success : actionArea.containsMouse ? Theme.accent : Theme.textMuted
        font: Theme.chatMeta
        Accessible.role: Accessible.Button
        Accessible.name: text
        MouseArea { id: actionArea; anchors.fill: parent; anchors.margins: -5; hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor; onClicked: action.trigger() }
    }

    // JARVIS's identity mark: the simplified Reactor-derived ring - a dark
    // circular housing, one controlled blue annulus with three subdued radial
    // cuts, a dark optical centre. 32 px, minimal bloom, inside a 36 px reserve
    // so nothing clips. ONE mark for the streaming turn and the finished reply,
    // so nothing changes under the reader when the answer stands.
    component JarvisMark: Item {
        width: 32; height: 32
        Rectangle { anchors.centerIn: parent; width: 36; height: 36; radius: 18; color: Theme.alpha("#36aaff", 0.09) }
        Rectangle { anchors.fill: parent; radius: 16; color: "#061424"; border.width: 1; border.color: "#2a5f8f" }
        Rectangle { anchors.centerIn: parent; width: 22; height: 22; radius: 11; color: "transparent"; border.width: 4; border.color: "#3aa8f0" }
        Repeater { model: 3
            Rectangle { width: 1.5; height: 8; color: "#061424"; x: 16 - 0.75; y: 16 - 11.5
                transform: Rotation { origin.x: 0.75; origin.y: 11.5; angle: 90 + index * 120 } } }
        Rectangle { anchors.centerIn: parent; width: 12; height: 12; radius: 6; color: "#03101c" }
    }

    // The entries already on screen, by id. A feed change rebuilds every row,
    // so a row makes its entrance only the first time its entry is shown -
    // never again for the history above a new entry. A reply that streamed and
    // a reopened conversation's turns are already there: no entrance either.
    property var shownIds: ({})
    // Q8: which sent quotes are unfolded, by entry id and quote index - kept here,
    // because a feed change rebuilds every row.
    property var unfolded: ({})
    function toggleUnfolded(key) { var next = Object.assign({}, unfolded); next[key] = !next[key]; unfolded = next }
    // The draft's quotes, read once for every row's marks (Q5).
    readonly property var draftGroups: bridge.contextGroups || []

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
                    readonly property string entryId: modelData.id
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
                    // Appearance: fade + short slide, no bounce, for a new entry only
                    // (feed.shownIds); any other row stands at once, before it is drawn.
                    // For inspection: whether this row made an entrance.
                    property bool entered: false
                    opacity: modelData.streamed === true ? 1 : 0
                    transform: Translate { id: slide; y: modelData.streamed === true ? 0 : Theme.travel }
                    Component.onCompleted: {
                        entered = modelData.streamed !== true && modelData.restored !== true && feed.shownIds[modelData.id] !== true
                        feed.shownIds[modelData.id] = true
                        if (entered) appear.start()
                        else { opacity = 1; slide.y = 0 }
                    }
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
                        // JARVIS: the identity mark.
                        JarvisMark {
                            objectName: row.isJarvis ? "feedAvatar_jarvis" : ""
                            visible: row.isJarvis
                            anchors.centerIn: parent
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
                        width: row.isUser ? (sentQuotes.visible ? maxUser : Math.min(maxUser, Math.ceil(probe.implicitWidth) + 2 * pad + 2))
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
                                // Q8: what the person quoted, before what they wrote - per quote
                                // its letter, the quoted words faint behind a left bar (a long
                                // quote folds: "Show more" / "Show less"), their comment and the
                                // files that quote owned; a fine line, then the common message.
                                Column {
                                    id: sentQuotes
                                    objectName: "sentQuotes"
                                    visible: row.isUser && (modelData.groups || []).length > 0
                                    width: parent.width
                                    spacing: 10
                                    Repeater {
                                        model: row.isUser ? (modelData.groups || []) : []
                                        Column {
                                            id: sentQuote
                                            objectName: "sentQuote_" + index
                                            readonly property string key: row.entryId + "/" + index
                                            readonly property bool expanded: feed.unfolded[key] === true
                                            readonly property var files: modelData.attachments || []
                                            width: sentQuotes.width
                                            spacing: 5
                                            Item {
                                                width: parent.width; height: Math.max(18, excerpt.height)
                                                Rectangle { width: 18; height: 18; radius: 9; y: 1
                                                            color: Theme.alpha("#36aaff", 0.2); border.width: 1; border.color: Theme.alpha("#49bdfa", 0.6)
                                                            Text { objectName: "sentQuoteLabel_" + index; anchors.centerIn: parent
                                                                   text: String.fromCharCode(65 + index); color: Theme.text; font: Theme.captionStrong } }
                                                Rectangle { x: 26; width: 2; height: parent.height; radius: 1; color: Theme.alpha(Theme.textMuted, 0.5) }
                                                Text { id: excerpt; objectName: "sentQuoteText_" + index
                                                       x: 36; width: parent.width - 36
                                                       text: String((modelData.reference || {}).text || ""); textFormat: Text.PlainText
                                                       wrapMode: Text.Wrap; maximumLineCount: sentQuote.expanded ? 100000 : 3; elide: Text.ElideRight
                                                       renderType: Theme.textRender; color: Theme.textMuted; font: Theme.body }
                                            }
                                            Text { objectName: "sentQuoteMore_" + index
                                                   visible: excerpt.truncated || sentQuote.expanded
                                                   x: 36; text: sentQuote.expanded ? qsTr("Show less") : qsTr("Show more")
                                                   color: moreArea.containsMouse ? Theme.accent : Theme.materialBlue; font: Theme.caption
                                                   Accessible.role: Accessible.Button; Accessible.name: text
                                                   MouseArea { id: moreArea; anchors.fill: parent; anchors.margins: -4; hoverEnabled: true
                                                               cursorShape: Qt.PointingHandCursor; onClicked: feed.toggleUnfolded(sentQuote.key) } }
                                            Text { objectName: "sentQuoteComment_" + index
                                                   visible: text !== ""; x: 36; width: parent.width - 36
                                                   text: String(modelData.note || ""); textFormat: Text.PlainText; wrapMode: Text.Wrap
                                                   renderType: Theme.textRender; color: Theme.text; font: Theme.body }
                                            Flow { objectName: "sentQuoteFiles_" + index
                                                   visible: sentQuote.files.length > 0; x: 36; width: parent.width - 36; spacing: 6
                                                   Repeater { model: sentQuote.files
                                                              AttachmentCard { file: modelData; compact: true; nested: true
                                                                               onOpened: bridge.previewAttachment(modelData) } } }
                                        }
                                    }
                                    Rectangle { width: parent.width; height: 1; color: Theme.alpha("#57b6e4", 0.2) }
                                }
                                TextEdit {
                                    id: message
                                    x: 0
                                    y: row.isJarvis ? name.y + name.height + 4 : sentQuotes.visible ? sentQuotes.height + 10 : 0
                                    width: row.isJarvis ? Math.min(parent.width, 760) : parent.width
                                    // Canonical text in, presented text out: the raw
                                    // assistant text is never rewritten for display.
                                    text: row.markdown ? feed.displayMarkdown(modelData.text) : modelData.text
                                    textFormat: row.markdown ? TextEdit.MarkdownText : TextEdit.PlainText
                                    renderType: Theme.textRender
                                    readOnly: true
                                    selectByMouse: true
                                    // A selection survives the click on "Quote".
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
                                // Q5: in the message a draft quote came from, each quoted range
                                // gets a thin dotted underline and its letter at its end; the
                                // letter opens that quote's detail. Drawn from the draft itself,
                                // so they leave with the quote: removed, or sent.
                                Item {
                                    id: quoteMarks
                                    objectName: "quoteMarks"
                                    x: message.x; y: message.y; width: message.width; height: message.height
                                    readonly property var marks: row.conversational
                                        ? feed.quoteMarks(message, modelData.id, feed.draftGroups, feed.quoteRanges, message.contentHeight, message.width) : []
                                    Repeater {
                                        model: quoteMarks.marks
                                        Item {
                                            id: mark
                                            objectName: "quoteMark_" + modelData.label
                                            readonly property var lines: modelData.lines
                                            Repeater {
                                                model: mark.lines
                                                // A dot every 3 px: a Canvas, drawn the same on every
                                                // scene-graph backend (a dashed Shape is not).
                                                Canvas {
                                                    objectName: "quoteUnderline"
                                                    x: modelData.x; y: modelData.y; width: modelData.w; height: 1
                                                    onWidthChanged: requestPaint()
                                                    onPaint: {
                                                        var ctx = getContext("2d")
                                                        ctx.clearRect(0, 0, width, height)
                                                        ctx.fillStyle = "rgba(87, 182, 228, 0.85)"      // #57b6e4; a Canvas reads CSS colours
                                                        for (var dot = 0; dot < width; dot += 3) ctx.fillRect(dot, 0, 1.5, 1)
                                                    }
                                                }
                                            }
                                            Rectangle {
                                                objectName: "quoteMarker_" + modelData.label
                                                // Raised like a superscript after the last word, on a
                                                // solid chip so it reads over the text it may touch.
                                                x: modelData.end.x + 2; y: modelData.end.y - 7
                                                width: 16; height: 16; radius: 8
                                                color: markerArea.containsMouse ? "#123a57" : "#0b2131"
                                                border.width: 1; border.color: Theme.alpha("#49bdfa", 0.85)
                                                Accessible.role: Accessible.Button; Accessible.name: modelData.label
                                                Text { anchors.centerIn: parent; text: modelData.label; color: Theme.text; font: Theme.label }
                                                HoverTip { text: qsTranslate("QuoteDetail", "Quote") + " " + modelData.label }
                                                MouseArea { id: markerArea; anchors.fill: parent; anchors.margins: -3; hoverEnabled: true
                                                            cursorShape: Qt.PointingHandCursor; onClicked: quoteDetail.openFor(modelData.id) }
                                            }
                                        }
                                    }
                                }
                                // What the person attached to this message, still visible after
                                // sending. `files` is every file with the letter of the group that
                                // owned it (none: the message's own); a quote's files are drawn in
                                // that quote's block above (Q8), so this strip draws the message's own.
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
                                    readonly property var own: files.filter(function(f) { return f.label === "" })
                                    visible: row.isUser && own.length > 0
                                    y: message.y + message.height + 8
                                    width: message.width
                                    spacing: 6
                                    Repeater {
                                        model: sent.own
                                        AttachmentCard { file: modelData.file; compact: true; nested: true
                                                         onOpened: bridge.previewAttachment(modelData.file) }
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
                        opacity: rowHover.hovered || message.selectedText.length > 0 || copyAction.confirming ? 1 : 0
                        Behavior on opacity { NumberAnimation { duration: Theme.hoverMs } }
                        spacing: 14
                        y: stamp.y
                        x: row.isUser ? stamp.x - width - 16 : (telemetry.visible ? telemetry.x + telemetry.width : stamp.x + stamp.width) + 16
                        MessageAction { id: copyAction; objectName: "messageCopy_" + index; label: qsTr("Copy"); confirmation: qsTr("Copied")
                                        onActivated: bridge.copyText(modelData.text) }
                        // Quoting adds a REFERENCE to the draft - it never writes into
                        // the message being composed, and it authorizes nothing.
                        MessageAction { objectName: "messageQuote_" + index; label: qsTr("Quote"); onActivated: bridge.quoteMessage(modelData.id) }
                        MessageAction { objectName: "messageQuoteSelection_" + index; label: qsTr("Quote selection")
                                        visible: message.selectedText.length > 0
                                        onActivated: feed.startQuote(message, modelData.id) }
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
                    JarvisMark { objectName: "streamAvatar"; anchors.centerIn: parent }
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
                    // The caret at the end of the growing text: only while text really
                    // arrives for the request that is running. A final, stopped or failed
                    // reply has none. Static: it marks the place, it does not blink.
                    Rectangle {
                        objectName: "streamCaret"
                        visible: streamRow.streaming && bridge.busy
                        readonly property rect end: { streamText.contentHeight; streamText.width
                                                      return streamText.positionToRectangle(streamText.length) }
                        x: end.x + 2; y: end.y + 3
                        width: 2; height: Math.max(8, end.height - 6); radius: 1
                        color: Theme.accent
                    }
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
                    opacity: streamHover.hovered || streamCopy.confirming ? 1 : 0
                    spacing: 14
                    anchors.verticalCenter: liveStatus.verticalCenter
                    x: liveStatus.x + liveStatus.width + 18
                    MessageAction { id: streamCopy; objectName: "streamCopy"; label: qsTr("Copy"); confirmation: qsTr("Copied")
                                    onActivated: bridge.copyText(bridge.streamText) }
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

    // -- the contextual quote action (Quoting v2) ---------------------------------
    // Selecting text inside a message JARVIS or the person wrote offers ONE small
    // "Quote" button right where the selection is (Q1). It follows the text
    // while the view scrolls, leaves as soon as the selection is cleared or the
    // conversation is rebuilt, and never interferes with selecting. Pressing it
    // takes the selection, snapped to whole words (Q11), as a new quote of the
    // draft and opens the quote popup there for an optional comment and files
    // (Q2'). Quotes are context only, never authority.

    // Q11: a half-selected word is taken whole. A letter or a digit is part of a
    // word, and so is an apostrophe between two of them (Turkish "OpenAI'ın").
    function wordAt(edit, i) {
        function plain(j) {
            if (j < 0 || j >= edit.length) return false
            var c = edit.getText(j, j + 1)
            return c.length === 1 && (c.toLowerCase() !== c.toUpperCase() || /[0-9_]/.test(c))
        }
        if (plain(i)) return true
        var c = i >= 0 && i < edit.length ? edit.getText(i, i + 1) : ""
        return (c === "'" || c === "\u2019") && plain(i - 1) && plain(i + 1)
    }
    function snapToWords(edit) {
        var s = Math.min(edit.selectionStart, edit.selectionEnd), e = Math.max(edit.selectionStart, edit.selectionEnd)
        var a = s, b = e
        while (a > 0 && wordAt(edit, a - 1) && wordAt(edit, a)) a--
        while (b < edit.length && wordAt(edit, b - 1) && wordAt(edit, b)) b++
        if (a !== s || b !== e) edit.select(a, b)
    }
    // A selection as the bridge stores it: the renderer's line marks are newlines.
    function quoteText(text) { return String(text).replace(/[\u2028\u2029]/g, "\n").trim() }
    function startQuote(edit, entryId) {
        if (!edit || edit.selectedText.length === 0) return
        snapToWords(edit)
        var text = edit.selectedText
        var start = Math.min(edit.selectionStart, edit.selectionEnd), end = Math.max(edit.selectionStart, edit.selectionEnd)
        var first = edit.positionToRectangle(start), last = edit.positionToRectangle(end)
        var before = (bridge.contextGroups || []).length
        hideQuoteTip()
        if (!bridge.quoteSelection(entryId, text)) { edit.deselect(); return }
        var groups = bridge.contextGroups || [], picked = null
        if (groups.length > before) {
            picked = groups[groups.length - 1]
            var ranges = Object.assign({}, quoteRanges)
            ranges[picked.id] = { entry: entryId, start: start, end: end }
            quoteRanges = ranges
        } else for (var i = 0; i < groups.length; i++) {       // the same quote again: its popup, to edit
            var at = quoteRanges[groups[i].id]
            if (at ? at.entry === entryId && at.start === start && at.end === end
                   : groups[i].reference.source_id === entryId && groups[i].reference.text === quoteText(text)) picked = groups[i]
        }
        edit.deselect()
        if (picked) quotePopup.openFor(edit, Qt.point(first.x, last.y + last.height + 6), picked.id,
                                       picked.reference.text, groups.length > before, picked.note)
    }

    // Where each selection quote was taken, by group id: the snapped range in its
    // message, so the same words twice mark the one quoted and a long quote (the
    // stored text is bounded) still ends where the selection ended.
    property var quoteRanges: ({})

    // Q5: where the draft's quotes of ONE message stand in it - per quote its id,
    // letter, underline segments (one per visual line) and the end point for its
    // letter. The range taken at quote time; else found by the quoted words in the
    // message's own text (a whole-message quote is all of it). A quote that cannot
    // be placed is not marked.
    function quoteMarks(edit, entryId, groups, ranges) {
        var out = [], shown = null
        for (var i = 0; i < (groups || []).length; i++) {
            var ref = groups[i].reference || {}
            if (ref.source_id !== entryId) continue
            if (shown === null) shown = edit.getText(0, edit.length).replace(/[\u2028\u2029]/g, "\n")
            var s = 0, e = shown.length, taken = (ranges || {})[groups[i].id]
            if (taken && taken.entry === entryId && taken.end <= shown.length) {
                s = taken.start; e = taken.end
            } else if (ref.selection === "partial") {
                s = shown.indexOf(String(ref.text || ""))
                if (s < 0 || !ref.text) continue
                e = s + ref.text.length
            } else {
                while (e > s && /\s/.test(shown.charAt(e - 1))) e--
            }
            var end = edit.positionToRectangle(e)
            out.push({ id: groups[i].id, label: groups[i].label, lines: rangeLines(edit, s, e),
                       end: { x: end.x, y: end.y + (end.height - 16) / 2 } })
        }
        return out
    }
    // One underline segment per visual line of [s, e): walked line by line, not
    // character by character, so a whole long message costs a few calls.
    function rangeLines(edit, s, e) {
        var out = [], p = s
        for (var guard = 0; p < e && guard < 400; guard++) {
            var r = edit.positionToRectangle(p)
            var lineEnd = edit.positionAt(edit.width, r.y + r.height / 2)
            var stop = Math.min(e, Math.max(lineEnd, p + 1))
            var q = edit.positionToRectangle(stop)
            var x2 = Math.abs(q.y - r.y) < 1 ? q.x : edit.positionToRectangle(stop - 1).x + 6
            if (x2 > r.x) out.push({ x: r.x, y: r.y + r.height - 1, w: x2 - r.x })
            var next = edit.positionAt(0, r.y + r.height + 2)
            p = next > stop ? next : stop + 1
        }
        return out
    }
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
        width: tipRow.width + 20; height: 26
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
        Row { id: tipRow; anchors.centerIn: parent; spacing: 6
              Text { anchors.verticalCenter: parent.verticalCenter; text: qsTr("Quote"); color: Theme.text; font: Theme.captionStrong }
              Icon { anchors.verticalCenter: parent.verticalCenter; name: "quote"; size: 12; tint: Theme.text } }
        MouseArea {
            id: tipArea
            objectName: "quoteTipButton"
            anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
            onClicked: { if (quoteTip.source) feed.startQuote(quoteTip.source, quoteTip.entryId); else feed.hideQuoteTip() }
        }
    }

    // Q2': the quote popup; Q9: a quote's detail, opened from its letter (Q5).
    QuotePopup { id: quotePopup }
    QuoteDetail { id: quoteDetail }
}
