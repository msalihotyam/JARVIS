import QtQuick
import QtQuick.Controls.Basic
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
    // Whether the approval card is on screen, for the conversation to bring it into view.
    readonly property bool approvalShown: approvalCard.visible
    readonly property real cardWidth: Math.min(760, width - inset)
    readonly property bool narrow: cardWidth < 560
    spacing: 16

    // The item Review focuses for an attention key: a card heading, never a button.
    function headingFor(key) {
        return key === "execution" ? executionHeading : key === "approval" ? approvalHeading
             : key === "outcome" ? outcomeHeading : null
    }

    // Core's side-effect codes as words; an unknown code stays as Core wrote it.
    function effectWord(code) {
        return ({ external_egress: qsTr("data leaves this computer"), local_write: qsTr("writes local files"), destructive: qsTr("destructive change"),
                  billing: qsTr("may incur cost"), push_deploy_publish: qsTr("pushes, deploys or publishes"),
                  financial_or_form_submit: qsTr("submits a form or payment"), read_only: qsTr("read only"), none: qsTr("no side effect") })[code] || code
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
        NumberAnimation { id: fade; target: surface; property: "opacity"; from: 0; to: 1; duration: Theme.dur(160); easing.type: Easing.OutCubic }
    }
    // A static state icon in a ring (Icon.qml): question unknown, close failed, check completed,
    // minus blocked, pause frozen, filled diamond approval.
    component StateIcon: Rectangle {
        property color tone: Theme.accentSoft
        property string icon: ""
        width: 40; height: 40; radius: width / 2
        color: Theme.alpha(tone, 0.10); border.width: 2; border.color: Theme.alpha(tone, 0.85)
        Icon { anchors.centerIn: parent; name: parent.icon; tint: parent.tone; size: Math.round(parent.width * 0.4) }
    }
    // One label / value field. Values wrap; nothing approval-critical is elided.
    component Field: Item {
        property string k: ""
        property string v: ""
        property color vColor: Theme.text
        width: parent ? parent.width : 0
        height: (cards.narrow ? key.height + 2 : 0) + value.height + 12
        Text { id: key; y: 6; width: cards.narrow ? parent.width : 118; text: parent.k; color: Theme.textLabel; font: Theme.uiLabel
               renderType: Theme.textRender }
        TextEdit { id: value; x: cards.narrow ? 0 : 130; y: cards.narrow ? key.height + 8 : 6; width: parent.width - x
                   text: parent.v; readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap; textFormat: TextEdit.PlainText
                   renderType: Theme.textRender
                   color: parent.vColor; font: Theme.uiValue }
        Rectangle { width: parent.width; height: 1; anchors.bottom: parent.bottom; color: Theme.glassSeparator }
    }
    // What Core attested beyond the scope (V2.0 Gate 6A): the project
    // instruction and project-file excerpts, complete and exactly as attested.
    // Inert plain text: no rich text, no links, no transformation; long
    // content scrolls inside its section.
    component AttestedText: Rectangle {
        id: section
        property string label: ""
        property string body: ""
        property string sectionName: ""
        width: parent ? parent.width : 0
        height: visible ? heading.height + Math.min(220, attested.contentHeight + 20) + 6 : 0
        color: "transparent"
        Text { id: heading; width: parent.width; text: parent.label; textFormat: Text.PlainText; wrapMode: Text.Wrap
               color: Theme.textMuted; font: Theme.label }
        Rectangle {
            y: heading.height + 6; width: parent.width; height: Math.min(220, attested.contentHeight + 20)
            radius: 7; color: Theme.surfaceDeep; border.width: 1; border.color: Theme.border
            Flickable {
                anchors.fill: parent; anchors.margins: 10; contentHeight: attested.contentHeight; clip: true
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                TextEdit { id: attested; objectName: section.sectionName; width: parent.width
                           text: section.body; readOnly: true; selectByMouse: true
                           textFormat: TextEdit.PlainText; wrapMode: TextEdit.WrapAnywhere
                           color: Theme.textLabel; font: Theme.code }
            }
        }
    }

    component Heading: Text {
        renderType: Theme.textRender
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
        // pause frozen / freezing (a pause, not a stop), exclamation unconfirmed or refused.
        StateIcon { x: 20; y: 16; tone: executionCard.tone; icon: executionCard.st === "frozen" || executionCard.st === "freeze_requested" ? "pause" : "exclamation" }
        Column {
            id: executionBody
            x: 76; y: 16; width: parent.width - 96; spacing: 6
            Heading { id: executionHeading; objectName: "safetyHeading_execution"; width: parent.width; color: executionCard.tone
                      text: ({ frozen: qsTr("JARVIS frozen"), freeze_requested: qsTr("Freeze requested — waiting for the current operation"),
                               unconfirmed: qsTr("JARVIS state unconfirmed"), refused: qsTr("Configuration refused — execution off") })[executionCard.st] || "" }
            Text { objectName: "safetyExecutionText"; renderType: Theme.textRender; width: parent.width; wrapMode: Text.Wrap; color: Theme.text
                   font: Theme.cardBody; lineHeightMode: Text.FixedHeight; lineHeight: 22
                   text: executionCard.st === "unconfirmed"
                         ? qsTr("The last request to freeze or resume JARVIS was not confirmed, so JARVIS does not show it as running or frozen until a request is confirmed.")
                         : executionCard.st === "refused" ? qsTr("The stored configuration was refused, so execution stays off. Repair it with: %1").arg("jarvis config reset --all")
                         : executionCard.st === "freeze_requested"
                           ? qsTr("Nothing new can start. The operation that is running now stops at its next safe point, then JARVIS is frozen. A step that is already under way - such as a message being sent - is not interrupted.")
                           : qsTr("Nothing new can start, and paused work stays where it stopped. JARVIS stays open: your conversation, results and history are here, and you can still reject a pending approval.") }
            Text { visible: executionCard.st === "frozen" || executionCard.st === "freeze_requested"; width: parent.width; wrapMode: Text.Wrap; color: Theme.textMuted; font: Theme.metadata
                   text: qsTr("Freezing does not undo anything already sent to its destination. Resume JARVIS from the red control or the tray.") }
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
        StateIcon { x: 20; y: 18; tone: Theme.warning; icon: "diamond-filled" }
        Column {
            id: approvalBody
            x: 76; y: 18; width: parent.width - 96; spacing: 8
            Heading { id: approvalHeading; objectName: "safetyHeading_approval"; width: parent.width; color: Theme.warning
                      text: approvalCard.a.complete ? qsTr("Approval required") : qsTr("Approval details unavailable") }
            Text { objectName: "safetyApprovalText"; width: parent.width; wrapMode: Text.Wrap; color: Theme.text; renderType: Theme.textRender
                   font: Theme.cardBody; lineHeightMode: Text.FixedHeight; lineHeight: 22
                   text: approvalCard.a.complete ? (approvalCard.a.summary || approvalCard.a.reason || qsTr("Review this action before authorizing it."))
                                                 : qsTr("Core's request is missing details needed to approve it safely, so approval is unavailable.") }
            Column {
                width: parent.width
                Field { k: qsTr("Action"); v: (approvalCard.a.action || qsTr("Not reported")) + (approvalCard.a.target ? " — " + approvalCard.a.target : "") }
                Field { k: qsTr("Connector"); v: approvalCard.a.connector || qsTr("Not reported") }
                Field { k: qsTr("Capability"); v: approvalCard.a.capability || qsTr("Not reported") }
                Field { k: qsTr("Destination"); v: approvalCard.a.destination || qsTr("Not reported"); vColor: Theme.accent }
                Field { k: qsTr("Authority"); v: approvalCard.a.data_classification ? qsTr("This request only · %1 data").arg(approvalCard.a.data_classification) : qsTr("This request only") }
                Field { visible: !!approvalCard.a.effect; k: qsTr("Consequence")
                        v: cards.effectWord(approvalCard.a.effect || "") + " · " + (approvalCard.a.reversible ? qsTr("reversible") : qsTr("not reversible")) }
            }
            AttestedText { visible: !!approvalCard.a.project_instruction; sectionName: "approvalInstruction"
                           label: qsTr("PROJECT INSTRUCTION · attested, sent with this request")
                           body: approvalCard.a.project_instruction || "" }
            Repeater {
                model: approvalCard.a.project_files || []
                AttestedText { sectionName: "approvalExcerpt"
                               label: modelData.chunk_index >= 0
                                      ? qsTr("PROJECT FILE EXCERPT · %1 · part %2 · %3").arg(modelData.name).arg(modelData.chunk_index + 1).arg(modelData.classification)
                                      : qsTr("PROJECT FILE EXCERPTS")
                               body: modelData.text }
            }
            // Review action details: what Core attached to the exact request.
            Item {
                width: parent.width; height: 36
                Text { id: detailsToggle; objectName: "approvalDetailsToggle"; anchors.verticalCenter: parent.verticalCenter
                       property bool open: false
                       text: (open ? qsTr("Hide action details") : qsTr("Review action details")) + "  ›"; color: Theme.accent; font: Theme.uiLabel
                       activeFocusOnTab: true; Accessible.role: Accessible.Button; Accessible.name: qsTr("Review action details")
                       Keys.onReturnPressed: open = !open; Keys.onSpacePressed: open = !open
                       MouseArea { anchors.fill: parent; anchors.margins: -8; cursorShape: Qt.PointingHandCursor; onClicked: detailsToggle.open = !detailsToggle.open } }
            }
            Column {
                visible: detailsToggle.open; width: parent.width
                Field { k: qsTr("Why"); v: approvalCard.a.reason || qsTr("Not reported") }
                Field { visible: !!approvalCard.a.outbound_preview; k: qsTr("Outbound"); v: approvalCard.a.outbound_preview || "" }
                Field { k: qsTr("Expires"); v: approvalCard.a.expires_at || qsTr("Not reported") }
                Field { k: qsTr("Request"); v: (approvalCard.a.request_id || qsTr("Not reported")) + "\n" + (approvalCard.a.fingerprint || "") ; vColor: Theme.textLabel }
            }
            Text { visible: !!approvalCard.a.hold; width: parent.width; wrapMode: Text.Wrap; color: Theme.warning; renderType: Theme.textRender
                   font: Theme.cardBody; lineHeightMode: Text.FixedHeight; lineHeight: 22; text: approvalCard.a.hold || "" }
            Flow {
                width: parent.width; spacing: 10
                ActionButton { objectName: "approvalApprove"; label: qsTr("Approve action"); primary: true; accent: Theme.warning; height: 44
                    width: cards.narrow ? parent.width : implicitWidth + 12; enabled: approvalCard.a.actionable === true
                    onClicked: bridge.approveRequest(approvalCard.a.request_id, approvalCard.a.fingerprint) }
                ActionButton { objectName: "approvalDeny"; label: qsTr("Reject"); height: 44
                    // Rejecting executes nothing, so it stays available while JARVIS is frozen.
                    width: cards.narrow ? parent.width : implicitWidth + 24; enabled: approvalCard.a.rejectable === true
                    onClicked: bridge.rejectRequest(approvalCard.a.request_id, approvalCard.a.fingerprint) }
            }
            Text { width: parent.width; wrapMode: Text.Wrap; color: Theme.textMuted; font: Theme.metadata
                   text: qsTr("Approve authorizes the displayed request only. It is not a completion claim.") }
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
                    icon: decisionCard.d.state === "recorded" ? "check" : decisionCard.d.state === "submitting" ? "more" : decisionCard.d.state === "rejected" ? "minus" : "exclamation" }
        Column {
            id: decisionBody
            x: 68; y: 14; width: parent.width - 88; spacing: 4
            Text { objectName: "safetyDecisionLabel"; width: parent.width; wrapMode: Text.Wrap; color: Theme.text; font: Theme.uiValue
                   text: decisionCard.d.label || "" }
            Text { width: parent.width; wrapMode: Text.Wrap; color: Theme.textMuted; font: Theme.metadata
                   text: [decisionCard.d.action, decisionCard.d.destination ? "→ " + decisionCard.d.destination : "", decisionCard.d.time].filter(function(x) { return !!x }).join("  ·  ")
                         + (decisionCard.d.state === "recorded" ? "\n" + qsTr("Execution and its outcome are reported separately.") : "")
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
        // Green is reserved for a confirmed, achieved objective. A run that ended
        // without achieving what was asked is amber, not success and not failure.
        tone: kind === "unknown" || kind === "in_progress" ? Theme.unknownEdge : kind === "failed" ? Theme.danger
            : kind === "blocked" || kind === "unmet" ? Theme.warning : kind === "completed" ? Theme.success : Theme.accentSoft
        height: visible ? outcomeBody.height + 32 : 0
        StateIcon { x: 20; y: 16; tone: outcomeCard.tone
                    icon: outcomeCard.kind === "unknown" || outcomeCard.kind === "in_progress" ? "question" : outcomeCard.kind === "failed" ? "close"
                         : outcomeCard.kind === "completed" ? "check" : outcomeCard.kind === "blocked" || outcomeCard.kind === "unmet" ? "minus"
                         : outcomeCard.kind === "paused" ? "pause" : "dot" }
        Column {
            id: outcomeBody
            x: 76; y: 16; width: parent.width - 96; spacing: 6
            Heading { id: outcomeHeading; objectName: "safetyHeading_outcome"; width: parent.width; color: outcomeCard.tone
                      text: ({ unknown: qsTr("Outcome unknown"), in_progress: qsTr("Outcome not established yet"), failed: qsTr("Failed"), blocked: qsTr("Blocked"), paused: qsTr("Paused by Freeze"),
                               completed: qsTr("Completed"), unmet: qsTr("Not completed"),
                               cancelled: outcomeCard.o.run_status === "cancelling" ? qsTr("Cancellation requested") : qsTr("Cancelled") })[outcomeCard.kind] || "" }
            Text { objectName: "safetyOutcomeText"; renderType: Theme.textRender; width: parent.width; wrapMode: Text.Wrap; color: Theme.text
                   font: Theme.cardBody; lineHeightMode: Text.FixedHeight; lineHeight: 22
                   text: outcomeCard.kind === "unmet" ? qsTr("The read-only access an inspection needs was not available, so what you asked for was not done. Nothing failed and nothing was changed.")
                       : outcomeCard.kind === "unknown" ? qsTr("JARVIS could not verify whether the action completed.")
                       : outcomeCard.kind === "in_progress" ? qsTr("This action is in progress elsewhere; its outcome is not known yet. Do not repeat it.")
                       : outcomeCard.kind === "paused" ? qsTr("JARVIS froze at a safe point between this task's steps. It was not failed or cancelled; Resume JARVIS continues it from where it stopped.")
                       // Core's own detail is appended as written: technical detail, not rewritten.
                       : outcomeCard.kind === "blocked" ? qsTr("This attempt did not proceed.") + (outcomeCard.o.message ? " " + outcomeCard.o.message : "")
                       : outcomeCard.kind === "failed" ? (outcomeCard.o.message || qsTr("Core reported a failure."))
                       : outcomeCard.kind === "completed" ? (outcomeCard.o.capability ? qsTr("Core confirmed completion of %1.").arg(outcomeCard.o.capability) : qsTr("Core confirmed completion."))
                       : outcomeCard.o.run_status === "cancelling" ? qsTr("Core recorded the cancellation request; the running action has not confirmed it yet.") : qsTr("This task was cancelled.") }
            Text { visible: outcomeCard.kind === "unknown" || outcomeCard.kind === "failed"; width: parent.width; wrapMode: Text.Wrap; color: Theme.textMuted; font: Theme.metadata
                   text: outcomeCard.kind === "unknown" ? qsTr("Unverified result. JARVIS does not repeat it automatically - repeating it may duplicate its effects.")
                                                        : qsTr("A confirmed failure does not prove there were no external effects.") }
            Item {
                visible: outcomeCard.kind !== "completed" && outcomeCard.kind !== "cancelled" && outcomeCard.kind !== "paused"; width: parent.width; height: visible ? 36 : 0
                Text { id: outcomeToggle; objectName: "outcomeDetailsToggle"; anchors.verticalCenter: parent.verticalCenter
                       property bool open: false
                       text: (open ? qsTr("Hide details") : outcomeCard.kind === "unknown" ? qsTr("Review outcome") : qsTr("View details")) + "  ›"; color: Theme.accent; font: Theme.uiLabel
                       activeFocusOnTab: true; Accessible.role: Accessible.Button; Accessible.name: text
                       Keys.onReturnPressed: open = !open; Keys.onSpacePressed: open = !open
                       MouseArea { anchors.fill: parent; anchors.margins: -8; cursorShape: Qt.PointingHandCursor; onClicked: outcomeToggle.open = !outcomeToggle.open } }
            }
            Column {
                visible: outcomeToggle.open && outcomeToggle.parent.visible; width: parent.width
                Field { k: qsTr("Action"); v: outcomeCard.o.capability || qsTr("Not reported") }
                Field { k: qsTr("Connector"); v: outcomeCard.o.connector || qsTr("Not reported") }
                Field { k: qsTr("Destination"); v: outcomeCard.o.destination || qsTr("Not reported") }
                Field { k: qsTr("Task"); v: outcomeCard.o.task_id || qsTr("Not reported"); vColor: Theme.textLabel }
                Field { k: qsTr("Core report"); v: [outcomeCard.o.code, outcomeCard.o.message].filter(function(x) { return !!x }).join(": ") || qsTr("Not reported") }
                Field { visible: outcomeCard.kind === "unknown"; k: qsTr("Resolution")
                        v: qsTr("Verify at the destination first. Operator resolution uses %1 / %2; there is no retry here.").arg("'jarvis task orphan'").arg("'jarvis task resolve'") }
            }
        }
    }

    Text {
        visible: cards.s.fixture === true
        x: cards.inset; width: cards.cardWidth; wrapMode: Text.Wrap; color: Theme.danger; font: Theme.metadata
        text: qsTr("DEV FIXTURE · placeholder fields, not a live request or outcome (Ctrl+Shift+S cycles scenarios)")
    }
}
