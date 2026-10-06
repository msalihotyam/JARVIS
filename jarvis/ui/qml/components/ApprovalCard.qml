import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// The exact backend snapshot is display-only. Actions still call the
// no-argument bridge slots; QML never constructs or selects a ticket.
GlassPanel {
    id: card
    property bool fixture: false
    readonly property var snapshot: fixture ? ({
        action:"analyse", target: qsTr("UI reconstruction — visual sample"), destination:"anthropic/claude-code",
        connector:"claude-code-plan", data_classification:"internal", effect:"external_egress", reversible:true,
        risk_level:"1", reason: qsTr("Display-only approval composition. No request or task has been created."),
        expires_at: qsTr("VISUAL FIXTURE · not an authorization"), session_id:"SAMPLE-SESSION", task_id:"SAMPLE-TASK",
        request_id:"SAMPLE-REQUEST", fingerprint: qsTr("SAMPLE — no approval fingerprint exists"),
        outbound_preview: qsTr("Review the interface composition and summarize the visual differences.\n\nThis text is a non-executable development sample."),
        attestation: qsTr("No attestation. This fixture cannot approve, deny or execute an action."),
        project_instruction: qsTr("[Project instruction, exactly as attested]"),
        project_files: [{ name: "notes.md", chunk_index: 0, classification: "internal", text: qsTr("[Project file excerpt, exactly as attested]") }]
    }) : bridge.approval
    readonly property bool actionable: !fixture && bridge.approvalPending && !bridge.busy
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

    signal dismissFixture()
    raised: true; padding: width < 600 ? 20 : 28; brackets: true
    border.color: Theme.alpha(Theme.warning, 0.65)
    objectName: "approvalCard"
    Item {
        id: head; width: parent.width; height: 72
        IconBox { x: 0; y: 2; width: 38; height: 40; icon: "diamond"; tint: Theme.warning }
        Column { x: 53; width: parent.width - 60; spacing: 5
            Text { width: parent.width; wrapMode: Text.Wrap; text: qsTr("AUTHORIZATION REQUIRED"); color: Theme.warning; font: Theme.pageHeading }
            Text { width: parent.width; wrapMode: Text.Wrap; text: card.fixture ? qsTr("VISUAL FIXTURE · no authority") : qsTr("HUMAN GATE · one action, exactly as shown"); color: Theme.textMuted; font: Theme.caption }
        }
        Rectangle { width: parent.width; height: 1; anchors.bottom: parent.bottom; color: Theme.alpha(Theme.warning, 0.22) }
    }
    Flickable {
        id: details
        anchors { left: parent.left; right: parent.right; top: head.bottom; bottom: actions.top; topMargin: 14; bottomMargin: 16 }
        contentHeight: fields.height; clip: true; boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
        Column {
            id: fields; width: parent.width; spacing: 12
            Text { width: parent.width; wrapMode: Text.Wrap; text: card.fixture ? qsTr("Design preview only. The decision controls are disabled.") : qsTr("This action is waiting for your decision. Review its scope and outbound content before approving."); color: Theme.textLabel; font: Theme.caption }
            Column { width: parent.width
                Repeater {
                    model: [
                        {k:"Action",label:qsTr("Action"),v:card.snapshot.action}, {k:"Target",label:qsTr("Target"),v:card.snapshot.target || qsTr("(none)")},
                        {k:"Destination",label:qsTr("Destination"),v:card.snapshot.destination}
                    ]
                    Item {
                        width: parent.width; height: Math.max(31, value.height + 16)
                        Text { x: 0; y: 8; text: modelData.label; color: Theme.textMuted; font: Theme.caption }
                        TextEdit { id: value; x: 112; y: 8; width: parent.width - 120; text: modelData.v || ""; readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap; textFormat: TextEdit.PlainText; color: modelData.k === "Destination" ? Theme.accent : Theme.text; font: Theme.caption }
                        Rectangle { width: parent.width; height: 1; anchors.bottom: parent.bottom; color: Theme.border; opacity: 0.5 }
                    }
                }
            }
            Text { visible: !!card.snapshot.outbound_preview; text: qsTr("OUTBOUND PREVIEW"); color: Theme.textMuted; font: Theme.label }
            Rectangle { visible: !!card.snapshot.outbound_preview; width: parent.width; height: visible ? Math.min(160, outbound.contentHeight + 28) : 0; radius: 7; color: Theme.surfaceDeep; border.width: 1; border.color: Theme.border
                Flickable { anchors.fill: parent; anchors.margins: 12; contentHeight: outbound.contentHeight; clip: true
                    TextEdit { id: outbound; objectName: "approvalOutbound"; width: parent.width; text: card.snapshot.outbound_preview || ""; readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap; textFormat: TextEdit.PlainText; color: Theme.textLabel; font: Theme.code }
                    ScrollBar.vertical: ScrollBar {}
                }
            }
            AttestedText { visible: !!card.snapshot.project_instruction; sectionName: "approvalInstruction"
                           label: qsTr("PROJECT INSTRUCTION · attested, sent with this request")
                           body: card.snapshot.project_instruction || "" }
            Repeater {
                model: card.snapshot.project_files || []
                AttestedText { sectionName: "approvalExcerpt"
                               label: modelData.chunk_index >= 0
                                      ? qsTr("PROJECT FILE EXCERPT · %1 · part %2 · %3").arg(modelData.name).arg(modelData.chunk_index + 1).arg(modelData.classification)
                                      : qsTr("PROJECT FILE EXCERPTS")
                               body: modelData.text }
            }
            Column { width: parent.width
                Repeater { model: [
                    {k:qsTr("Connector"),v:card.snapshot.connector}, {k:qsTr("Data class"),v:card.snapshot.data_classification},
                    {k:qsTr("Effect"),v:(card.snapshot.effect || "") + " · " + (card.snapshot.reversible ? qsTr("reversible") : qsTr("NOT reversible"))},
                    {k:qsTr("Risk level"),v:card.snapshot.risk_level}, {k:qsTr("Reason"),v:card.snapshot.reason}, {k:qsTr("Expires"),v:card.snapshot.expires_at}
                ]
                    Item { width: parent.width; height: Math.max(28, detail.height + 12)
                        Text { y: 6; text: modelData.k; color: Theme.textMuted; font: Theme.caption }
                        TextEdit { id: detail; x: 112; y: 6; width: parent.width - 120; text: modelData.v || ""; readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap; textFormat: TextEdit.PlainText; color: Theme.text; font: Theme.caption }
                        Rectangle { width: parent.width; height: 1; anchors.bottom: parent.bottom; color: Theme.border; opacity: 0.5 }
                    }
                }
            }
            Text { text: qsTr("EXACT REQUEST BINDING"); color: Theme.textMuted; font: Theme.label }
            Column { width: parent.width; spacing: 6
                Repeater { model: [{k:qsTr("Session"),v:card.snapshot.session_id},{k:qsTr("Task"),v:card.snapshot.task_id},{k:qsTr("Request"),v:card.snapshot.request_id},{k:qsTr("Fingerprint"),v:card.snapshot.fingerprint}]
                    Item { width: parent.width; height: Math.max(17, identity.height)
                        Text { text: modelData.k; color: Theme.textMuted; font: Theme.caption }
                        TextEdit { id: identity; x: 90; width: parent.width - 94; text: modelData.v || ""; readOnly: true; selectByMouse: true; wrapMode: TextEdit.WrapAnywhere; textFormat: TextEdit.PlainText; color: Theme.textLabel; font: Theme.code }
                    }
                }
            }
            ActionButton { id: technical; property bool expanded: false; label: (expanded ? "− " : "+ ") + qsTr("Technical attestation"); compact: true; visible: !!card.snapshot.attestation; onClicked: expanded = !expanded }
            TextEdit { width: parent.width; visible: technical.expanded; height: visible ? contentHeight : 0; text: card.snapshot.attestation || ""; readOnly: true; selectByMouse: true; wrapMode: TextEdit.WrapAnywhere; textFormat: TextEdit.PlainText; color: Theme.textMuted; font: Theme.code }
        }
    }
    Item {
        id: actions; anchors { left: parent.left; right: parent.right; bottom: parent.bottom } height: 44
        Row { anchors.right: parent.right; spacing: 10
            ActionButton { objectName: "approvalDeny"; label: qsTr("Deny"); danger: true; enabled: card.actionable; onClicked: bridge.deny() }
            ActionButton { objectName: "approvalCancel"; label: qsTr("Cancel task"); enabled: card.actionable; onClicked: bridge.cancelTask() }
            ActionButton { objectName: "approvalApprove"; label: qsTr("Approve"); primary: true; enabled: card.actionable; onClicked: bridge.approve() }
        }
        IconButton { visible: card.fixture; icon: "close"; tip: qsTr("Close visual fixture"); onClicked: card.dismissFixture() }
    }
}
