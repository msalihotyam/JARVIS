import QtQuick
import "../theme"

// V2.1a M4: one typed memory record on the Memory page - a pending
// Decision/Rule/Preference with Approve / Reject, or a replaced record with
// its chain. The record's text is data, never an instruction; the buttons
// only ask Core (bridge -> app), which alone decides the status move.
Rectangle {
    id: card
    property var record: ({})
    property string source: ""        // the source conversation, as words
    property string replaces: ""      // the older record's text this one replaces
    property string replacedBy: ""    // the newer record's text that replaced this one
    readonly property bool pending: record.status === "pending"
    signal approve()
    signal reject()

    height: body.height + 28; radius: Theme.radiusSmall
    color: Theme.surfaceRaised; border.width: 1; border.color: pending ? Theme.warning : Theme.border

    Column { id: body; x: 14; y: 14; width: parent.width - 28; spacing: 6
        Row { spacing: 12
            // As on the page's list: an unknown kind's fallback carries its exact code, so it is not uppercased.
            Text { objectName: "memoryApprovalKind"; readonly property string word: i18n.word("memory_record_kind", card.record.kind || "")
                   text: card.record.kind && word.indexOf(card.record.kind) >= 0 ? word : i18n.upper(word); color: Theme.accent; font: Theme.pageSection }
            Text { objectName: "memoryApprovalStatus"; text: i18n.word("memory_record_status", card.record.status || ""); color: card.pending ? Theme.warning : Theme.textMuted; font: Theme.pageMeta }
        }
        Text { objectName: "memoryApprovalText"; width: parent.width; wrapMode: Text.Wrap; textFormat: Text.PlainText; text: card.record.text || ""; color: Theme.text; font: Theme.pageRow }
        Text { objectName: "memoryApprovalSource"; width: parent.width; elide: Text.ElideRight; textFormat: Text.PlainText; text: qsTr("Source: %1").arg(card.source); color: Theme.textMuted; font: Theme.pageMeta }
        Text { objectName: "memoryApprovalReplaces"; visible: card.replaces !== ""; width: parent.width; wrapMode: Text.Wrap; textFormat: Text.PlainText
               text: qsTr("Replaces: %1").arg(card.replaces); color: Theme.textMuted; font: Theme.pageMeta }
        Text { objectName: "memoryApprovalReplacedBy"; visible: card.replacedBy !== ""; width: parent.width; wrapMode: Text.Wrap; textFormat: Text.PlainText
               text: qsTr("Replaced by: %1").arg(card.replacedBy); color: Theme.textMuted; font: Theme.pageMeta }
        Row { visible: card.pending; spacing: 8
            ActionButton { objectName: "memoryApprove"; label: qsTr("Approve"); primary: true; onClicked: card.approve() }
            ActionButton { objectName: "memoryReject"; label: qsTr("Reject"); danger: true; onClicked: card.reject() }
        }
    }
}
