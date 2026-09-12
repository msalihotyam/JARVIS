import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// The exact backend snapshot is display-only. Actions still call the
// no-argument bridge slots; QML never constructs or selects a ticket.
GlassPanel {
    id: card
    property bool fixture: false
    readonly property var snapshot: fixture ? ({
        action:"analyse", target:"UI reconstruction — visual sample", destination:"anthropic/claude-code",
        connector:"claude-code-plan", data_classification:"internal", effect:"external_egress", reversible:true,
        risk_level:"1", reason:"Display-only approval composition. No request or task has been created.",
        expires_at:"VISUAL FIXTURE · not an authorization", session_id:"SAMPLE-SESSION", task_id:"SAMPLE-TASK",
        request_id:"SAMPLE-REQUEST", fingerprint:"SAMPLE — no approval fingerprint exists",
        outbound_preview:"Review the interface composition and summarize the visual differences.\n\nThis text is a non-executable development sample.",
        attestation:"No attestation. This fixture cannot approve, deny or execute an action."
    }) : bridge.approval
    readonly property bool actionable: !fixture && bridge.approvalPending && !bridge.busy
    signal dismissFixture()
    raised: true; padding: width < 600 ? 20 : 28; brackets: true
    border.color: Theme.alpha(Theme.warning, 0.65)
    objectName: "approvalCard"
    Item {
        id: head; width: parent.width; height: 72
        IconBox { x: 0; y: 2; width: 38; height: 40; glyph: "◇"; tint: Theme.warning }
        Column { x: 53; width: parent.width - 60; spacing: 5
            Text { width: parent.width; wrapMode: Text.Wrap; text: "AUTHORIZATION REQUIRED"; color: Theme.warning; font { family: Theme.font; pixelSize: card.width < 600 ? 13 : 15; letterSpacing: 2; weight: Font.DemiBold } }
            Text { width: parent.width; wrapMode: Text.Wrap; text: card.fixture ? "VISUAL FIXTURE · no authority" : "HUMAN GATE · one action, exactly as shown"; color: Theme.textMuted; font { family: Theme.font; pixelSize: 11 } }
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
            Text { width: parent.width; wrapMode: Text.Wrap; text: card.fixture ? "Design preview only. The decision controls are disabled." : "This action is waiting for your decision. Review its scope and outbound content before approving."; color: Theme.textLabel; font { family: Theme.font; pixelSize: 13 } }
            Column { width: parent.width
                Repeater {
                    model: [
                        {k:"Action",v:card.snapshot.action}, {k:"Target",v:card.snapshot.target || "(none)"},
                        {k:"Destination",v:card.snapshot.destination}
                    ]
                    Item {
                        width: parent.width; height: Math.max(31, value.height + 16)
                        Text { x: 0; y: 8; text: modelData.k; color: Theme.textMuted; font { family: Theme.font; pixelSize: 12 } }
                        TextEdit { id: value; x: 112; y: 8; width: parent.width - 120; text: modelData.v || ""; readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap; textFormat: TextEdit.PlainText; color: modelData.k === "Destination" ? Theme.accent : Theme.text; font { family: Theme.font; pixelSize: 13 } }
                        Rectangle { width: parent.width; height: 1; anchors.bottom: parent.bottom; color: Theme.border; opacity: 0.5 }
                    }
                }
            }
            Text { visible: !!card.snapshot.outbound_preview; text: "OUTBOUND PREVIEW"; color: Theme.textMuted; font { family: Theme.font; pixelSize: 10; letterSpacing: 1.8 } }
            Rectangle { visible: !!card.snapshot.outbound_preview; width: parent.width; height: visible ? Math.min(160, outbound.contentHeight + 28) : 0; radius: 7; color: Theme.surfaceDeep; border.width: 1; border.color: Theme.border
                Flickable { anchors.fill: parent; anchors.margins: 12; contentHeight: outbound.contentHeight; clip: true
                    TextEdit { id: outbound; objectName: "approvalOutbound"; width: parent.width; text: card.snapshot.outbound_preview || ""; readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap; textFormat: TextEdit.PlainText; color: Theme.textLabel; font { family: Theme.mono; pixelSize: 12 } }
                    ScrollBar.vertical: ScrollBar {}
                }
            }
            Column { width: parent.width
                Repeater { model: [
                    {k:"Connector",v:card.snapshot.connector}, {k:"Data class",v:card.snapshot.data_classification},
                    {k:"Effect",v:(card.snapshot.effect || "") + (card.snapshot.reversible ? " · reversible" : " · NOT reversible")},
                    {k:"Risk level",v:card.snapshot.risk_level}, {k:"Reason",v:card.snapshot.reason}, {k:"Expires",v:card.snapshot.expires_at}
                ]
                    Item { width: parent.width; height: Math.max(28, detail.height + 12)
                        Text { y: 6; text: modelData.k; color: Theme.textMuted; font { family: Theme.font; pixelSize: 12 } }
                        TextEdit { id: detail; x: 112; y: 6; width: parent.width - 120; text: modelData.v || ""; readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap; textFormat: TextEdit.PlainText; color: Theme.text; font { family: Theme.font; pixelSize: 13 } }
                        Rectangle { width: parent.width; height: 1; anchors.bottom: parent.bottom; color: Theme.border; opacity: 0.5 }
                    }
                }
            }
            Text { text: "EXACT REQUEST BINDING"; color: Theme.textMuted; font { family: Theme.font; pixelSize: 10; letterSpacing: 1.8 } }
            Column { width: parent.width; spacing: 6
                Repeater { model: [{k:"Session",v:card.snapshot.session_id},{k:"Task",v:card.snapshot.task_id},{k:"Request",v:card.snapshot.request_id},{k:"Fingerprint",v:card.snapshot.fingerprint}]
                    Item { width: parent.width; height: Math.max(17, identity.height)
                        Text { text: modelData.k; color: Theme.textMuted; font { family: Theme.font; pixelSize: 11 } }
                        TextEdit { id: identity; x: 90; width: parent.width - 94; text: modelData.v || ""; readOnly: true; selectByMouse: true; wrapMode: TextEdit.WrapAnywhere; textFormat: TextEdit.PlainText; color: Theme.textLabel; font { family: Theme.mono; pixelSize: 10 } }
                    }
                }
            }
            ActionButton { id: technical; property bool expanded: false; label: expanded ? "− Technical attestation" : "+ Technical attestation"; compact: true; visible: !!card.snapshot.attestation; onClicked: expanded = !expanded }
            TextEdit { width: parent.width; visible: technical.expanded; height: visible ? contentHeight : 0; text: card.snapshot.attestation || ""; readOnly: true; selectByMouse: true; wrapMode: TextEdit.WrapAnywhere; textFormat: TextEdit.PlainText; color: Theme.textMuted; font { family: Theme.mono; pixelSize: 11 } }
        }
    }
    Item {
        id: actions; anchors { left: parent.left; right: parent.right; bottom: parent.bottom } height: 44
        Row { anchors.right: parent.right; spacing: 10
            ActionButton { objectName: "approvalDeny"; label: "Deny"; danger: true; enabled: card.actionable; onClicked: bridge.deny() }
            ActionButton { objectName: "approvalCancel"; label: "Cancel task"; enabled: card.actionable; onClicked: bridge.cancelTask() }
            ActionButton { objectName: "approvalApprove"; label: "Approve"; primary: true; enabled: card.actionable; onClicked: bridge.approve() }
        }
        IconButton { visible: card.fixture; glyph: "✕"; tip: "Close visual fixture"; onClicked: card.dismissFixture() }
    }
}
