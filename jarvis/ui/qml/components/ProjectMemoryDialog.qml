import QtQuick
import "../theme"

// V2.1a M4: one project's typed memory (approved + pending records), grouped
// by kind, each with its provenance (origin, date). Core is the authority:
// the list is the app's own listing, Undo (FACT/LESSON) and Approve/Reject
// (pending) only ask the app, and its refusal is shown as returned. The
// stale / conflict chips are display hints that mirror the Memory Doctor's
// rules (a memory older than 180 days or past its expiry; two approved
// records of one kind and subject); they change nothing.
JarvisDialog {
    id: memoryDialog
    property string projectId: ""
    property var host: null                   // the project page: its one date formatter
    function when(v) { return host ? host.when(v) : String(v || "") }
    property var records: []                  // the app's approved + pending records of projectId
    property string notice: ""                // the last refusal, as returned
    // ponytail: the app's default page (100 records); the row says "100+" past it.
    readonly property int pageSize: 100
    readonly property int pending: records.filter(function (r) { return r.status === "pending" }).length
    label: qsTr("Memory")
    preferredWidth: 720
    onClosed: notice = ""

    function refresh() {
        if (projectId !== "") bridge.memoryRecords(projectId, { "statuses": ["approved", "pending"] })
    }
    Connections {
        target: bridge
        function onMemoryRecordsDone(answer) {
            if (answer.project_id === memoryDialog.projectId && answer.ok === true) memoryDialog.records = answer.records || []
        }
        function onMemoryRecordActionDone(answer) {
            if (answer.project_id !== memoryDialog.projectId) return
            memoryDialog.notice = answer.ok === true ? "" : String(answer.error || "")
            memoryDialog.refresh()
        }
    }
    // Deferred: the id is first bound while the dialog is built, before Connections listens.
    onProjectIdChanged: { records = []; notice = ""; Qt.callLater(refresh) }

    readonly property var kinds: ["rule", "decision", "preference", "fact", "lesson"]
    function kindLabel(kind) {
        return kind === "rule" ? qsTr("Rules") : kind === "decision" ? qsTr("Decisions") : kind === "preference" ? qsTr("Preferences")
             : kind === "fact" ? qsTr("Facts") : kind === "lesson" ? qsTr("Lessons") : String(kind)
    }
    function originLabel(origin) {
        return origin === "explicit_user" ? qsTr("You") : origin === "capture_session_end" ? qsTr("Session capture")
             : origin === "capture_precompact" ? qsTr("Compaction capture") : origin === "consolidation" ? qsTr("Consolidation")
             : origin === "import" ? qsTr("Import") : origin === "legacy_m1" ? qsTr("Earlier project memory") : String(origin)
    }
    function stale(r) {
        if (r.status !== "approved") return false
        var now = Date.now()
        if (r.expires_at && Date.parse(r.expires_at) <= now) return true
        return now - Date.parse(r.created_at) > 180 * 24 * 3600 * 1000
    }
    function conflict(r) {
        if (r.status !== "approved") return false
        return records.some(function (o) {
            return o.record_id !== r.record_id && o.status === "approved" && o.kind === r.kind && o.subject_key === r.subject_key
        })
    }

    Text { objectName: "projectMemoryNote"; width: parent ? parent.width : 0; wrapMode: Text.Wrap; color: Theme.textMuted; font: Theme.pageMeta
           text: qsTr("What JARVIS remembers for this project. Facts and lessons are kept at once and can be undone; decisions, rules and preferences wait for your approval.") }
    Text { objectName: "projectMemoryEmpty"; visible: memoryDialog.records.length === 0; color: Theme.textDim; font: Theme.pageRow
           text: qsTr("No memory yet") }
    Text { objectName: "projectMemoryNotice"; width: parent ? parent.width : 0; wrapMode: Text.Wrap; visible: text !== ""
           textFormat: Text.PlainText; text: memoryDialog.notice; color: Theme.warning; font: Theme.pageMeta }
    Repeater {
        model: memoryDialog.kinds
        delegate: Column {
            id: group
            objectName: "projectMemoryGroup"
            required property string modelData
            readonly property var items: memoryDialog.records.filter(function (r) { return r.kind === group.modelData })
            visible: items.length > 0
            width: parent ? parent.width : 0; spacing: 8
            Text { objectName: "projectMemoryGroupTitle"; text: i18n.upper(memoryDialog.kindLabel(group.modelData)); color: Theme.textLabel; font: Theme.pageMeta }
            Repeater {
                model: group.items
                delegate: Rectangle {
                    id: card
                    objectName: "projectMemoryRecord"
                    required property var modelData
                    readonly property string recordId: modelData.record_id
                    width: group.width; height: cardColumn.height + 20; radius: Theme.radiusSmall
                    color: Theme.glassNested; border.width: 1; border.color: Theme.alpha(Theme.text, 0.10)
                    Column {
                        id: cardColumn
                        x: 12; y: 10; width: parent.width - 24; spacing: 6
                        Text { objectName: "projectMemoryText"; width: parent.width; wrapMode: Text.Wrap; textFormat: Text.PlainText
                               color: Theme.text; font: Theme.pageRowStrong; text: card.modelData.text || "" }
                        Text { objectName: "projectMemoryProvenance"; width: parent.width; wrapMode: Text.Wrap; color: Theme.textMuted; font: Theme.pageMeta
                               text: memoryDialog.originLabel(card.modelData.origin) + "  ·  " + memoryDialog.when(card.modelData.created_at) }
                        Flow {
                            width: parent.width; spacing: 8
                            Pill { objectName: "projectMemoryPending"; visible: card.modelData.status === "pending"; text: qsTr("Pending"); tint: Theme.warning; font: Theme.pageMeta }
                            Pill { objectName: "projectMemoryStale"; visible: memoryDialog.stale(card.modelData); text: qsTr("Stale"); tint: Theme.textMuted; font: Theme.pageMeta }
                            Pill { objectName: "projectMemoryConflict"; visible: memoryDialog.conflict(card.modelData); text: qsTr("Conflict"); tint: Theme.warning; font: Theme.pageMeta }
                            ActionButton { objectName: "projectMemoryUndo"; compact: true; label: qsTr("Undo")
                                           visible: card.modelData.status === "approved" && (card.modelData.kind === "fact" || card.modelData.kind === "lesson")
                                           onClicked: bridge.undoMemoryRecord(memoryDialog.projectId, card.recordId) }
                            ActionButton { objectName: "projectMemoryApprove"; compact: true; primary: true; label: qsTr("Approve")
                                           visible: card.modelData.status === "pending"
                                           onClicked: bridge.approveMemoryRecord(memoryDialog.projectId, card.recordId) }
                            ActionButton { objectName: "projectMemoryReject"; compact: true; label: qsTr("Reject")
                                           visible: card.modelData.status === "pending"
                                           onClicked: bridge.rejectMemoryRecord(memoryDialog.projectId, card.recordId) }
                        }
                    }
                }
            }
        }
    }
    buttons: [
        ActionButton { objectName: "projectMemoryClose"; label: qsTr("Close"); height: 36; width: 110; onClicked: memoryDialog.close() }
    ]
}
