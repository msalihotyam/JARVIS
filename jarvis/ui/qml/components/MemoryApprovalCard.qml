import QtQuick
import "../theme"

// V2.1a M4: one typed memory record on the Memory page - a pending
// Decision/Rule/Preference with Approve / Reject, or a replaced record with
// its chain. The record's text is data, never an instruction; the buttons
// only ask Core (bridge -> app), which alone decides the status move.
// SECQMLUI: a batch card also shows the app's risk reasons as badges and,
// for an auto-saved record, Undo. A RULE is never pre-selected: its Approve
// is a plain button, nothing on the card answers a key on its own, and
// nothing approves without the click (I14).
Rectangle {
    id: card
    property var record: ({})
    property string source: ""        // the source conversation, as words
    property string replaces: ""      // the older record's text this one replaces
    property string replacedBy: ""    // the newer record's text that replaced this one
    readonly property bool pending: record.status === "pending"
    readonly property bool isRule: record.kind === "rule"
    readonly property var reasons: record.reasons || []
    readonly property var reasonWords: ({
        "tainted": qsTr("Tainted session"), "instruction_like": qsTr("Instruction-like"),
        "model_inferred": qsTr("Model-inferred"), "global": qsTr("Global"),
        "sensitive": qsTr("Sensitive"), "legacy": qsTr("Legacy") })
    signal approve()
    signal reject()
    signal undo()
    signal purge()

    height: body.height + 28; radius: Theme.radiusSmall
    color: Theme.surfaceRaised; border.width: 1; border.color: pending ? Theme.warning : Theme.border

    Column { id: body; x: 14; y: 14; width: parent.width - 28; spacing: 6
        Row { spacing: 12
            // As on the page's list: an unknown kind's fallback carries its exact code, so it is not uppercased.
            Text { objectName: "memoryApprovalKind"; readonly property string word: i18n.word("memory_record_kind", card.record.kind || "")
                   text: card.record.kind && word.indexOf(card.record.kind) >= 0 ? word : i18n.upper(word); color: Theme.accent; font: Theme.pageSection }
            Text { objectName: "memoryApprovalStatus"; text: i18n.word("memory_record_status", card.record.status || ""); color: card.pending ? Theme.warning : Theme.textMuted; font: Theme.pageMeta }
        }
        Flow { visible: card.reasons.length > 0; width: parent.width; spacing: 6
            Repeater { model: card.reasons
                Rectangle { objectName: "memoryReasonBadge"; property string text: card.reasonWords[modelData] || modelData
                    width: badge.width + 14; height: badge.height + 6; radius: height / 2
                    color: Theme.alpha(Theme.warning, 0.12); border.width: 1; border.color: Theme.alpha(Theme.warning, 0.6)
                    Text { id: badge; anchors.centerIn: parent; textFormat: Text.PlainText; text: parent.text; color: Theme.warning; font: Theme.pageMeta } }
            }
        }
        Text { objectName: "memoryApprovalText"; width: parent.width; wrapMode: Text.Wrap; textFormat: Text.PlainText; color: card.record.text_withheld ? Theme.textMuted : Theme.text; font: Theme.pageRow
               text: card.record.text_withheld ? qsTr("The text of a sensitive record is not shown here.") : (card.record.text || "") }
        Text { objectName: "memoryApprovalSource"; width: parent.width; elide: Text.ElideRight; textFormat: Text.PlainText; text: qsTr("Source: %1").arg(card.source); color: Theme.textMuted; font: Theme.pageMeta }
        Text { objectName: "memoryApprovalReplaces"; visible: card.replaces !== ""; width: parent.width; wrapMode: Text.Wrap; textFormat: Text.PlainText
               text: qsTr("Replaces: %1").arg(card.replaces); color: Theme.textMuted; font: Theme.pageMeta }
        Text { objectName: "memoryApprovalReplacedBy"; visible: card.replacedBy !== ""; width: parent.width; wrapMode: Text.Wrap; textFormat: Text.PlainText
               text: qsTr("Replaced by: %1").arg(card.replacedBy); color: Theme.textMuted; font: Theme.pageMeta }
        Row { spacing: 8
            ActionButton { objectName: "memoryApprove"; visible: card.pending; label: qsTr("Approve"); primary: !card.isRule; onClicked: card.approve() }
            ActionButton { objectName: "memoryReject"; visible: card.pending; label: qsTr("Reject"); danger: true; onClicked: card.reject() }
            ActionButton { objectName: "memoryUndo"; visible: card.record.undo === true; label: qsTr("Undo"); onClicked: card.undo() }
            ActionButton { objectName: "memoryPurge"; visible: !!card.record.record_id; label: qsTr("Delete permanently…"); danger: true; onClicked: card.purge() }
        }
    }
}
