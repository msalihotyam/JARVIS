import QtQuick
import "../theme"

// The conversation's safety cards, at the end of the stream on the JARVIS
// side: the execution notice, the approval request (or the receipt of the
// decision on it) and the current task's outcome. Everything is projected
// from bridge.safety - Core-reported state only. Approve and Reject act on
// the exact request shown (its id and fingerprint); nothing here retries,
// resolves an unknown outcome or changes execution.
Column {
    id: cards
    objectName: "safetyCards"
    property real inset: 0                      // the JARVIS message column
    readonly property var s: bridge.safety
    readonly property real cardWidth: Math.min(760, width - inset)
    readonly property bool narrow: cardWidth < 560
    spacing: 16

    // The item Review focuses for an attention key: a card heading, never a button.
    function headingFor(key) {
        return key === "execution" ? executionHeading : key === "approval" ? approvalHeading
             : key === "outcome" ? outcomeHeading : null
    }

    function toneColor(tone) {
        return tone === "danger" ? Theme.danger : tone === "warning" ? Theme.warning
             : tone === "success" ? Theme.success : tone === "unknown" ? Theme.unknownEdge : Theme.accentSoft
    }

    // -- shared pieces --------------------------------------------------------
    component Surface: Rectangle {
        id: surface
        property color tone: Theme.accentSoft
        x: cards.inset; width: cards.cardWidth
        radius: 12; color: Theme.glassPrimaryReading
        border.width: 1; border.color: Theme.alpha(tone, 0.55)
        // A static structural accent on the leading edge: shape and label carry meaning, not colour alone.
        Rectangle { x: 0; y: 10; width: 3; height: surface.height - 20; radius: 1.5; color: surface.tone; opacity: 0.85 }
        // A short opacity entrance when the card appears; nothing loops.
        onVisibleChanged: if (visible) { opacity = 0; fade.restart() }
        NumberAnimation { id: fade; target: surface; property: "opacity"; from: 0; to: 1; duration: 160; easing.type: Easing.OutCubic }
    }
    // A static state glyph in a ring: ? unknown, ✕ failed, ✓ completed, – blocked, ■ disabled, ◆ approval.
    component StateIcon: Rectangle {
        property color tone: Theme.accentSoft
        property string glyph: ""
        width: 40; height: 40; radius: width / 2
        color: Theme.alpha(tone, 0.10); border.width: 2; border.color: Theme.alpha(tone, 0.85)
        Text { anchors.centerIn: parent; text: parent.glyph; color: parent.tone; font { family: Theme.font; pixelSize: 19; weight: Font.DemiBold } }
    }
    // One label / value field. Values wrap; nothing approval-critical is elided.
    component Field: Item {
        property string k: ""
        property string v: ""
        property color vColor: Theme.text
        width: parent ? parent.width : 0
        height: (cards.narrow ? key.height + 2 : 0) + value.height + 12
        Text { id: key; y: 6; width: cards.narrow ? parent.width : 118; text: parent.k; color: Theme.textLabel; font: Theme.uiLabel }
        TextEdit { id: value; x: cards.narrow ? 0 : 130; y: cards.narrow ? key.height + 8 : 6; width: parent.width - x
                   text: parent.v; readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap; textFormat: TextEdit.PlainText
                   color: parent.vColor; font: Theme.uiValue }
        Rectangle { width: parent.width; height: 1; anchors.bottom: parent.bottom; color: Theme.glassSeparator }
    }
    component Heading: Text {
        activeFocusOnTab: true
        Accessible.role: Accessible.Heading
        Accessible.name: text
        wrapMode: Text.Wrap
        font: Theme.sectionHeading
        Rectangle { anchors.fill: parent; anchors.margins: -4; radius: 4; color: "transparent"; border.width: 1; border.color: Theme.accent; visible: parent.activeFocus }
    }

    // -- execution authority notice -------------------------------------------
    Surface {
        id: executionCard
        objectName: "safetyCard_execution"
        readonly property string st: cards.s.execution || ""
        visible: !cards.s.fixture && (st === "frozen" || st === "freeze_requested" || st === "refused" || st === "unconfirmed")
        tone: st === "freeze_requested" ? Theme.warning : Theme.danger
        height: visible ? executionBody.height + 32 : 0
        // ❚❚ frozen / freezing (a pause, not a stop), ! unconfirmed or refused.
        StateIcon { x: 20; y: 16; tone: executionCard.tone; glyph: executionCard.st === "frozen" || executionCard.st === "freeze_requested" ? "❚❚" : "!" }
        Column {
            id: executionBody
            x: 76; y: 16; width: parent.width - 96; spacing: 6
            Heading { id: executionHeading; objectName: "safetyHeading_execution"; width: parent.width; color: executionCard.tone
                      text: ({ frozen: "JARVIS frozen", freeze_requested: "Freeze requested — waiting for the current operation",
                               unconfirmed: "JARVIS state unconfirmed", refused: "Configuration refused — execution off" })[executionCard.st] || "" }
            Text { objectName: "safetyExecutionText"; width: parent.width; wrapMode: Text.Wrap; color: Theme.text; font: Theme.uiLabel
                   text: executionCard.st === "unconfirmed"
                         ? "The last request to freeze or resume JARVIS was not confirmed, so JARVIS does not show it as running or frozen until a request is confirmed."
                         : executionCard.st === "refused" ? "The stored configuration was refused, so execution stays off. Repair it with: jarvis config reset --all"
                         : executionCard.st === "freeze_requested"
                           ? "Nothing new can start. The operation that is running now stops at its next safe point, then JARVIS is frozen. A step that is already under way - such as a message being sent - is not interrupted."
                           : "Nothing new can start, and paused work stays where it stopped. JARVIS stays open: your conversation, results and history are here, and you can still reject a pending approval." }
            Text { visible: executionCard.st === "frozen" || executionCard.st === "freeze_requested"; width: parent.width; wrapMode: Text.Wrap; color: Theme.textMuted; font: Theme.metadata
                   text: "Freezing does not undo anything already sent to its destination. Resume JARVIS from the red control or the tray." }
        }
    }

    // -- the approval request ---------------------------------------------------
    Surface {
        id: approvalCard
        objectName: "safetyCard_approval"
        readonly property var a: cards.s.approval || ({})
        visible: Object.keys(a).length > 0
        tone: Theme.warning
        border.width: 1.5
        height: visible ? approvalBody.height + 36 : 0
        StateIcon { x: 20; y: 18; tone: Theme.warning; glyph: "◆" }
        Column {
            id: approvalBody
            x: 76; y: 18; width: parent.width - 96; spacing: 8
            Heading { id: approvalHeading; objectName: "safetyHeading_approval"; width: parent.width; color: Theme.warning
                      text: approvalCard.a.complete ? "Approval required" : "Approval details unavailable" }
            Text { width: parent.width; wrapMode: Text.Wrap; color: Theme.text; font: Theme.uiLabel
                   text: approvalCard.a.complete ? (approvalCard.a.summary || approvalCard.a.reason || "Review this action before authorizing it.")
                                                 : "Core's request is missing details needed to approve it safely, so approval is unavailable." }
            Column {
                width: parent.width
                Field { k: "Action"; v: (approvalCard.a.action || "Not reported") + (approvalCard.a.target ? " — " + approvalCard.a.target : "") }
                Field { k: "Connector"; v: approvalCard.a.connector || "Not reported" }
                Field { k: "Capability"; v: approvalCard.a.capability || "Not reported" }
                Field { k: "Destination"; v: approvalCard.a.destination || "Not reported"; vColor: Theme.accent }
                Field { k: "Authority"; v: "This request only" + (approvalCard.a.data_classification ? " · " + approvalCard.a.data_classification + " data" : "") }
                Field { visible: !!approvalCard.a.effect; k: "Consequence"
                        v: (approvalCard.a.effect || "") + (approvalCard.a.reversible ? " · reversible" : " · not reversible") }
            }
            // Review action details: what Core attached to the exact request.
            Item {
                width: parent.width; height: 36
                Text { id: detailsToggle; objectName: "approvalDetailsToggle"; anchors.verticalCenter: parent.verticalCenter
                       property bool open: false
                       text: (open ? "Hide action details" : "Review action details") + "  ›"; color: Theme.accent; font: Theme.uiLabel
                       activeFocusOnTab: true; Accessible.role: Accessible.Button; Accessible.name: "Review action details"
                       Keys.onReturnPressed: open = !open; Keys.onSpacePressed: open = !open
                       MouseArea { anchors.fill: parent; anchors.margins: -8; cursorShape: Qt.PointingHandCursor; onClicked: detailsToggle.open = !detailsToggle.open } }
            }
            Column {
                visible: detailsToggle.open; width: parent.width
                Field { k: "Why"; v: approvalCard.a.reason || "Not reported" }
                Field { visible: !!approvalCard.a.outbound_preview; k: "Outbound"; v: approvalCard.a.outbound_preview || "" }
                Field { k: "Expires"; v: approvalCard.a.expires_at || "Not reported" }
                Field { k: "Request"; v: (approvalCard.a.request_id || "Not reported") + "\n" + (approvalCard.a.fingerprint || "") ; vColor: Theme.textLabel }
            }
            Text { visible: !!approvalCard.a.hold; width: parent.width; wrapMode: Text.Wrap; color: Theme.warning; font: Theme.uiLabel; text: approvalCard.a.hold || "" }
            Flow {
                width: parent.width; spacing: 10
                ActionButton { objectName: "approvalApprove"; label: "Approve action"; primary: true; accent: Theme.warning; height: 44
                    width: cards.narrow ? parent.width : implicitWidth + 12; enabled: approvalCard.a.actionable === true
                    onClicked: bridge.approveRequest(approvalCard.a.request_id, approvalCard.a.fingerprint) }
                ActionButton { objectName: "approvalDeny"; label: "Reject"; height: 44
                    // Rejecting executes nothing, so it stays available while JARVIS is frozen.
                    width: cards.narrow ? parent.width : implicitWidth + 24; enabled: approvalCard.a.rejectable === true
                    onClicked: bridge.rejectRequest(approvalCard.a.request_id, approvalCard.a.fingerprint) }
            }
            Text { width: parent.width; wrapMode: Text.Wrap; color: Theme.textMuted; font: Theme.metadata
                   text: "Approve authorizes the displayed request only. It is not a completion claim." }
        }
    }

    // -- the decision receipt -----------------------------------------------------
    Surface {
        id: decisionCard
        objectName: "safetyCard_decision"
        readonly property var d: cards.s.decision || ({})
        visible: !!d.state
        tone: d.state === "recorded" ? Theme.success : d.state === "rejected" ? Theme.accentSoft
            : d.state === "submitting" ? Theme.accent : Theme.warning
        height: visible ? decisionBody.height + 28 : 0
        StateIcon { x: 20; y: 14; width: 32; height: 32; tone: decisionCard.tone
                    glyph: decisionCard.d.state === "recorded" ? "✓" : decisionCard.d.state === "submitting" ? "…" : decisionCard.d.state === "rejected" ? "–" : "!" }
        Column {
            id: decisionBody
            x: 68; y: 14; width: parent.width - 88; spacing: 4
            Text { objectName: "safetyDecisionLabel"; width: parent.width; wrapMode: Text.Wrap; color: Theme.text; font: Theme.uiValue
                   text: decisionCard.d.label || "" }
            Text { width: parent.width; wrapMode: Text.Wrap; color: Theme.textMuted; font: Theme.metadata
                   text: [decisionCard.d.action, decisionCard.d.destination ? "→ " + decisionCard.d.destination : "", decisionCard.d.time].filter(function(x) { return !!x }).join("  ·  ")
                         + (decisionCard.d.state === "recorded" ? "\nExecution and its outcome are reported separately." : "")
                         // Why a decision was not recorded; a recorded one's execution report belongs to the outcome.
                         + (decisionCard.d.message && decisionCard.d.state !== "recorded" ? "\n" + decisionCard.d.message : "") }
        }
    }

    // -- the current task's outcome -------------------------------------------------
    Surface {
        id: outcomeCard
        objectName: "safetyCard_outcome"
        readonly property var o: cards.s.outcome || ({})
        readonly property string kind: o.kind || ""
        visible: kind !== ""
        tone: kind === "unknown" || kind === "in_progress" ? Theme.unknownEdge : kind === "failed" ? Theme.danger
            : kind === "blocked" ? Theme.warning : kind === "completed" ? Theme.success : Theme.accentSoft
        height: visible ? outcomeBody.height + 32 : 0
        StateIcon { x: 20; y: 16; tone: outcomeCard.tone
                    glyph: outcomeCard.kind === "unknown" || outcomeCard.kind === "in_progress" ? "?" : outcomeCard.kind === "failed" ? "✕"
                         : outcomeCard.kind === "completed" ? "✓" : outcomeCard.kind === "blocked" ? "—" : outcomeCard.kind === "paused" ? "❚❚" : "·" }
        Column {
            id: outcomeBody
            x: 76; y: 16; width: parent.width - 96; spacing: 6
            Heading { id: outcomeHeading; objectName: "safetyHeading_outcome"; width: parent.width; color: outcomeCard.tone
                      text: ({ unknown: "Outcome unknown", in_progress: "Outcome not established yet", failed: "Failed", blocked: "Blocked", paused: "Paused by Freeze",
                               completed: "Completed", cancelled: outcomeCard.o.run_status === "cancelling" ? "Cancellation requested" : "Cancelled" })[outcomeCard.kind] || "" }
            Text { objectName: "safetyOutcomeText"; width: parent.width; wrapMode: Text.Wrap; color: Theme.text; font: Theme.uiLabel
                   text: outcomeCard.kind === "unknown" ? "JARVIS could not verify whether the action completed."
                       : outcomeCard.kind === "in_progress" ? "This action is in progress elsewhere; its outcome is not known yet. Do not repeat it."
                       : outcomeCard.kind === "paused" ? "JARVIS froze at a safe point between this task's steps. It was not failed or cancelled; Resume JARVIS continues it from where it stopped."
                       : outcomeCard.kind === "blocked" ? "This attempt did not proceed." + (outcomeCard.o.message ? " " + outcomeCard.o.message : "")
                       : outcomeCard.kind === "failed" ? (outcomeCard.o.message || "Core reported a failure.")
                       : outcomeCard.kind === "completed" ? "Core confirmed completion" + (outcomeCard.o.capability ? " of " + outcomeCard.o.capability : "") + "."
                       : outcomeCard.o.run_status === "cancelling" ? "Core recorded the cancellation request; the running action has not confirmed it yet." : "This task was cancelled." }
            Text { visible: outcomeCard.kind === "unknown" || outcomeCard.kind === "failed"; width: parent.width; wrapMode: Text.Wrap; color: Theme.textMuted; font: Theme.metadata
                   text: outcomeCard.kind === "unknown" ? "Unverified result. JARVIS does not repeat it automatically - repeating it may duplicate its effects."
                                                        : "A confirmed failure does not prove there were no external effects." }
            Item {
                visible: outcomeCard.kind !== "completed" && outcomeCard.kind !== "cancelled" && outcomeCard.kind !== "paused"; width: parent.width; height: visible ? 36 : 0
                Text { id: outcomeToggle; objectName: "outcomeDetailsToggle"; anchors.verticalCenter: parent.verticalCenter
                       property bool open: false
                       text: (open ? "Hide details" : outcomeCard.kind === "unknown" ? "Review outcome" : "View details") + "  ›"; color: Theme.accent; font: Theme.uiLabel
                       activeFocusOnTab: true; Accessible.role: Accessible.Button; Accessible.name: text
                       Keys.onReturnPressed: open = !open; Keys.onSpacePressed: open = !open
                       MouseArea { anchors.fill: parent; anchors.margins: -8; cursorShape: Qt.PointingHandCursor; onClicked: outcomeToggle.open = !outcomeToggle.open } }
            }
            Column {
                visible: outcomeToggle.open && outcomeToggle.parent.visible; width: parent.width
                Field { k: "Action"; v: outcomeCard.o.capability || "Not reported" }
                Field { k: "Connector"; v: outcomeCard.o.connector || "Not reported" }
                Field { k: "Destination"; v: outcomeCard.o.destination || "Not reported" }
                Field { k: "Task"; v: outcomeCard.o.task_id || "Not reported"; vColor: Theme.textLabel }
                Field { k: "Core report"; v: [outcomeCard.o.code, outcomeCard.o.message].filter(function(x) { return !!x }).join(": ") || "Not reported" }
                Field { visible: outcomeCard.kind === "unknown"; k: "Resolution"
                        v: "Verify at the destination first. Operator resolution uses 'jarvis task orphan' / 'jarvis task resolve'; there is no retry here." }
            }
        }
    }

    Text {
        visible: cards.s.fixture === true
        x: cards.inset; width: cards.cardWidth; wrapMode: Text.Wrap; color: Theme.danger; font: Theme.metadata
        text: "DEV FIXTURE · placeholder fields, not a live request or outcome (Ctrl+Shift+S cycles scenarios)"
    }
}
