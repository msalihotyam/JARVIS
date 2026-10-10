import QtQuick
import "../theme"

// Gate 6G Sidebar v2 V10: a SABİTLENENLER row's info card. Hover a pinned
// row and wait (`delay`): the card opens to the RIGHT of the row with the
// conversation's stored facts (bridge.pinInfo) - the kind and project, the
// title, the project's instruction line or the first message, the project's
// active files, the last change. Real data only: a fact that is empty or
// unreadable is left out, nothing is generated. The pointer leaving the row,
// a press, a drag or the row's menu closes it. It selects nothing.
MenuSurface {
    id: card
    objectName: "pinInfoCard"
    property Item row: null
    property string conversationId: ""
    property var info: ({})
    property int delay: 500
    readonly property bool project: (info.project_id || "") !== ""
    readonly property var files: info.files || []

    width: 300
    padding: 12
    takesFocus: false                       // information only: the focus stays where it is

    // The pointer is on `target` (the pin `id`): the card opens after the wait.
    function hoverRow(target, id) {
        if (row !== target) close()
        row = target; conversationId = id
        wait.restart()
    }
    function leave() { wait.stop(); close() }
    Timer {
        id: wait
        interval: card.delay
        onTriggered: {
            if (!card.row || !card.row.hovered || card.row.dragging) return
            card.info = bridge.pinInfo(card.conversationId)
            if (card.info.title === undefined) return
            // Right of the row, its top at the row's top (kept inside the window).
            card.popupAt(card.row, Qt.point(card.row.width + 8, 0))
        }
    }

    Column {
        width: card.availableWidth
        spacing: 6
        Row {
            spacing: 6
            Icon { anchors.verticalCenter: parent.verticalCenter; size: 14; name: card.project ? "folder" : "chat"
                   tint: card.project ? Theme.alpha(Theme.accent, 0.75) : Theme.textLabel }
            Text { objectName: "pinInfoKind"; anchors.verticalCenter: parent.verticalCenter; width: card.availableWidth - 20
                   textFormat: Text.PlainText; elide: Text.ElideRight; color: Theme.textMuted; font: Theme.caption
                   text: card.project ? qsTr("Project conversation · %1").arg(card.info.project_name || "")
                                      : qsTr("Conversation without a project") }
        }
        Text { objectName: "pinInfoTitle"; width: card.availableWidth; textFormat: Text.PlainText
               wrapMode: Text.Wrap; maximumLineCount: 2; elide: Text.ElideRight
               text: card.info.title || qsTr("New Chat"); color: card.info.title ? Theme.text : Theme.textLabel; font: Theme.bodyStrong }
        Column {
            objectName: "pinInfoSummary"
            readonly property string text: card.project ? card.info.instruction || "" : card.info.first_message || ""
            visible: text !== ""
            width: card.availableWidth; spacing: 2
            Text { text: card.project ? qsTr("Instruction") : qsTr("First message"); color: Theme.textDim; font: Theme.caption }
            Text { objectName: "pinInfoSummaryText"; width: parent.width; textFormat: Text.PlainText
                   wrapMode: Text.Wrap; maximumLineCount: 3; elide: Text.ElideRight
                   text: parent.text; color: Theme.text; font: Theme.caption }
        }
        Column {
            objectName: "pinInfoFiles"
            visible: card.files.length > 0
            width: card.availableWidth; spacing: 2
            Text { objectName: "pinInfoFileCount"; color: Theme.textDim; font: Theme.caption
                   text: card.files.length === 1 ? qsTr("1 file") : qsTr("%1 files").arg(card.files.length) }
            Text { objectName: "pinInfoFileNames"; width: parent.width; textFormat: Text.PlainText
                   wrapMode: Text.Wrap; maximumLineCount: 2; elide: Text.ElideRight
                   text: card.files.join(", "); color: Theme.text; font: Theme.caption }
        }
        Text { objectName: "pinInfoUpdated"; visible: text !== ""; width: card.availableWidth; textFormat: Text.PlainText
               color: Theme.textMuted; font: Theme.captionFigures
               text: { var at = card.info.updated_at ? i18n.when(card.info.updated_at, Date.now(), "time") : ""
                       return at !== "" ? qsTr("Last activity: %1").arg(at) : "" } }
    }
}
