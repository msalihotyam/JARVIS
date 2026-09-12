import QtQuick
import QtQuick.Controls.Basic
import "../theme"
import "../components"

// HUMAN_GATE. Exists only while the backend holds a pending approval
// ticket; shows exactly the persisted request; Approve / Deny hand that
// exact ticket back, and the backend re-proves it.
Item {
    id: overlay
    anchors.fill: parent
    readonly property bool pending: bridge.approvalPending
    visible: pending || dim.opacity > 0
    z: 300

    Rectangle {
        id: dim
        anchors.fill: parent
        color: "#000000"
        opacity: overlay.pending ? 0.62 : 0
        Behavior on opacity { NumberAnimation { duration: overlay.pending ? Theme.approvalIn : Theme.approvalOut; easing.type: Easing.OutCubic } }
        MouseArea { anchors.fill: parent; enabled: overlay.pending }
    }

    GlassPanel {
        id: card
        raised: true
        anchors.centerIn: parent
        width: Math.min(parent.width * 0.6, 860); height: Math.min(parent.height * 0.82, body.height + 120)
        border.color: Theme.warning
        opacity: overlay.pending ? 1 : 0
        scale: overlay.pending ? 1 : 0.97
        transform: Translate { y: overlay.pending ? 0 : 6; Behavior on y { NumberAnimation { duration: Theme.approvalIn; easing.type: Easing.OutCubic } } }
        Behavior on opacity { NumberAnimation { duration: overlay.pending ? Theme.approvalIn : Theme.approvalOut; easing.type: Easing.OutCubic } }
        Behavior on scale { NumberAnimation { duration: overlay.pending ? Theme.approvalIn : Theme.approvalOut; easing.type: Easing.OutCubic } }
        padding: 26

        Column {
            id: body
            width: parent.width
            spacing: 14
            Row { spacing: 14
                IconBox { width: 36; height: 36; glyph: "⚑"; tint: Theme.warning; anchors.verticalCenter: parent.verticalCenter }
                Column { anchors.verticalCenter: parent.verticalCenter; spacing: 3
                    Text { text: "AUTHORIZATION REQUIRED"; color: Theme.warning; font { family: Theme.font; pixelSize: 13; letterSpacing: 3; weight: Font.DemiBold } }
                    Text { text: "HUMAN GATE  ·  one action, exactly as shown"; color: Theme.textMuted; font { family: Theme.font; pixelSize: 10; letterSpacing: 1.6 } }
                }
            }
            Text { text: "JARVIS will not act until you decide. Nothing has executed."; color: Theme.textMuted; font { family: Theme.font; pixelSize: 13 } }
            Grid {
                columns: 2; columnSpacing: 18; rowSpacing: 6; width: parent.width
                component K: Text { color: Theme.textMuted; font { family: Theme.font; pixelSize: 12 } }
                component V: Text { color: Theme.text; width: body.width - 190; wrapMode: Text.Wrap; font { family: Theme.font; pixelSize: 13 } }
                K { text: "Action" }        V { text: bridge.approval.action || "" }
                K { text: "Target" }        V { text: bridge.approval.target || "(none)" }
                K { text: "Destination" }   V { text: bridge.approval.destination || ""; color: Theme.accent }
                K { text: "Connector" }     V { text: bridge.approval.connector || "" }
                K { text: "Data class" }    V { text: bridge.approval.data_classification || "" }
                K { text: "Effect" }        V { text: (bridge.approval.effect || "") + (bridge.approval.reversible ? "  ·  reversible" : "  ·  NOT reversible") }
                K { text: "Risk level" }    V { text: bridge.approval.risk_level || "" }
                K { text: "Reason" }        V { text: bridge.approval.reason || "" }
                K { text: "Expires" }       V { text: bridge.approval.expires_at || "" }
                K { text: "Request" }       V { text: bridge.approval.request_id || ""; font.family: Theme.mono; font.pixelSize: 11 }
                K { text: "Fingerprint" }   V { text: bridge.approval.fingerprint || ""; font.family: Theme.mono; font.pixelSize: 11 }
                K { text: "Task" }          V { text: bridge.approval.task_id || ""; font.family: Theme.mono; font.pixelSize: 11 }
            }
            Text { visible: bridge.approval.outbound_preview !== ""; text: "OUTBOUND PREVIEW"; color: Theme.textMuted; font { family: Theme.font; pixelSize: 10; letterSpacing: 2 } }
            ScrollView {
                visible: bridge.approval.outbound_preview !== ""
                width: parent.width; height: Math.min(160, previewText.contentHeight + 12)
                TextArea { id: previewText; readOnly: true; selectByMouse: true; text: bridge.approval.outbound_preview || ""; color: Theme.text; wrapMode: TextEdit.Wrap; background: Rectangle { color: Theme.surface; radius: 6 }  font { family: Theme.mono; pixelSize: 11 } }
            }
            Text { visible: bridge.approval.attestation !== ""; text: "ATTESTATION"; color: Theme.textMuted; font { family: Theme.font; pixelSize: 10; letterSpacing: 2 } }
            ScrollView {
                visible: bridge.approval.attestation !== ""
                width: parent.width; height: Math.min(120, attText.contentHeight + 12)
                TextArea { id: attText; readOnly: true; selectByMouse: true; text: bridge.approval.attestation || ""; color: Theme.textMuted; wrapMode: TextEdit.Wrap; background: Rectangle { color: Theme.surface; radius: 6 }  font { family: Theme.mono; pixelSize: 11 } }
            }
            Row {
                spacing: 12
                anchors.right: parent.right
                ActionButton { label: "Deny"; danger: true; enabled: !bridge.busy; onClicked: bridge.deny() }
                ActionButton { label: "Cancel task"; enabled: !bridge.busy; onClicked: bridge.cancelTask() }
                ActionButton { label: "Approve"; primary: true; enabled: !bridge.busy; onClicked: bridge.approve() }
            }
        }
    }
}
