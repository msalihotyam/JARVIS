import QtQuick
import QtQuick.Controls.Basic
import "../theme"
import "../components"

// Project memory: explicit, untrusted reference data. Read-only here; it
// is not chat and grants no authority.
SecondaryPage {
    id: page
    title: qsTr("Memory")
    property string project: ""
    property bool includeInactive: false
    headerRight: [
        ActionButton { label: page.includeInactive ? qsTr("Active only") : qsTr("Include superseded / retired"); onClicked: { page.includeInactive = !page.includeInactive; bridge.refreshMemory(page.project, page.includeInactive) } },
        ActionButton { label: qsTr("Load"); primary: true; onClicked: { bridge.refreshMemory(page.project, page.includeInactive); page.loadRecords() } }
    ]
    Component.onCompleted: { bridge.refreshMemory(page.project, page.includeInactive); page.loadRecords() }

    // V2.1a M4: the typed records of the shown project (bridge.memoryRecords). Pending
    // Decisions/Rules/Preferences wait for the user's Approve / Reject; Core decides the move.
    property var records: []
    readonly property string scope: {
        if (page.project !== "") return page.project
        var active = bridge.projectRecords.filter(function(p) { return p.active })
        return active.length ? active[0].id : ""
    }
    readonly property var pending: records.filter(function(r) { return r.status === "pending" && ["decision", "rule", "preference"].indexOf(r.kind) >= 0 })
    readonly property var replaced: records.filter(function(r) { return r.status === "superseded" })
    onScopeChanged: loadRecords()
    function loadRecords() { if (page.scope !== "") bridge.memoryRecords(page.scope, {}) }
    function textOf(id) { var r = page.records.find(function(x) { return x.record_id === id }); return r ? r.text : "" }
    function newerOf(id) { var r = page.records.find(function(x) { return x.supersedes === id }); return r ? r.text : "" }
    function sourceOf(record) {
        var id = record.source_conversation_id || record.conversation_id || ""
        if (id === "") return qsTr("No source conversation")
        var c = bridge.conversations.find(function(x) { return x.id === id })
        return c && c.title ? c.title : qsTr("A conversation in this project")
    }
    Connections {
        target: bridge
        function onMemoryRecordsDone(answer) { if (answer.ok && answer.project_id === page.scope) page.records = answer.records || [] }
        function onMemoryRecordActionDone(answer) { if (answer.project_id === page.scope) page.loadRecords() }
    }

    Column {
        anchors.fill: parent
        spacing: 12
        Text { text: qsTr("Project memory is explicit reference data written only by you. It is shown as data: nothing here is an instruction to JARVIS."); color: Theme.textLabel; width: parent.width; wrapMode: Text.Wrap; font: Theme.pageRow }
        Row {
            spacing: 8
            Text { anchors.verticalCenter: parent.verticalCenter; text: qsTr("PROJECT"); color: Theme.textLabel; font: Theme.pageSection }
            // Names are shown; the id stays internal (Gate 3 ids are opaque).
            Repeater {
                model: bridge.projectRecords.filter(function(p) { return p.active })
                ActionButton { objectName: "memoryProject"; label: modelData.name; primary: modelData.id === page.project || (page.project === "" && index === 0); onClicked: { page.project = modelData.id; bridge.refreshMemory(modelData.id, page.includeInactive) } }
            }
        }
        Column { objectName: "memoryPending"; width: parent.width; spacing: 8; visible: page.scope !== ""
            Text { text: qsTr("AWAITING YOUR APPROVAL"); color: Theme.textLabel; font: Theme.pageSection }
            Text { objectName: "memoryPendingEmpty"; visible: page.pending.length === 0; width: parent.width; wrapMode: Text.Wrap
                   text: qsTr("No decision, rule or preference is waiting for your approval."); color: Theme.textMuted; font: Theme.pageMeta }
            Repeater {
                model: page.pending
                MemoryApprovalCard { objectName: "memoryApprovalCard"; width: parent.width; record: modelData
                    source: page.sourceOf(modelData); replaces: modelData.supersedes ? page.textOf(modelData.supersedes) : ""
                    onApprove: bridge.approveMemoryRecord(page.scope, modelData.record_id)
                    onReject: bridge.rejectMemoryRecord(page.scope, modelData.record_id) }
            }
        }
        // Replaced records stay visible with their chain (I8: nothing is deleted).
        Column { objectName: "memoryReplaced"; width: parent.width; spacing: 8; visible: page.replaced.length > 0
            Text { text: qsTr("REPLACED"); color: Theme.textLabel; font: Theme.pageSection }
            Repeater {
                model: page.replaced
                MemoryApprovalCard { objectName: "memoryChain"; width: parent.width; record: modelData
                    source: page.sourceOf(modelData); replacedBy: page.newerOf(modelData.record_id) }
            }
        }
        // K6: the honest empty states - no project to hold memory, or a project with none yet.
        PageEmpty { objectName: "memoryEmpty"; width: parent.width; visible: bridge.memoryItems.length === 0
                    title: bridge.projects.length === 0 ? qsTr("No project yet, so there is no project memory.") : qsTr("No memory records to show for this project.")
                    note: bridge.projects.length === 0 ? qsTr("Memory belongs to a project. Create one on the Projects page.")
                          : page.includeInactive ? qsTr("Records appear here once something is saved to this project's memory.")
                          : qsTr("Records appear here once something is saved to this project's memory. Replaced or retired ones, if any, show with the button above.")
                    action: bridge.projects.length === 0 ? qsTr("Open Projects") : ""
                    onActed: bridge.setPage("projects") }
        ListView {
            width: parent.width; height: Math.max(0, parent.height - y); clip: true; spacing: 8; visible: count > 0
            model: bridge.memoryItems
            ScrollBar.vertical: ScrollBar {}
            delegate: Rectangle {
                width: ListView.view.width; height: body.height + 28; radius: Theme.radiusSmall
                color: Theme.surfaceRaised; border.width: 1; border.color: Theme.border
                Column { id: body; x: 14; y: 14; width: parent.width - 28; spacing: 6
                    Row { spacing: 12
                        // The kind's word as a heading; an unknown kind's fallback carries its exact code, so it is not uppercased.
                        Text { readonly property string word: i18n.word("memory_kind", modelData.kind || "")
                               text: modelData.kind && word.indexOf(modelData.kind) >= 0 ? word : i18n.upper(word); color: Theme.accent; font: Theme.pageSection }
                        Text { objectName: "memoryClassification"; text: i18n.word("memory_classification", modelData.classification || ""); color: Theme.textLabel; font: Theme.pageMeta }
                        Text { objectName: "memoryLifecycle"; text: i18n.word("memory_lifecycle", modelData.lifecycle || ""); color: modelData.active ? Theme.success : Theme.textMuted; font: Theme.pageMeta }
                        Text { objectName: "memoryOrigin"; text: i18n.word("memory_origin", modelData.origin || ""); color: Theme.textMuted; font: Theme.pageMeta }
                        Text { text: page.when(modelData.created_at); color: Theme.textMuted; font: Theme.pageMetaFigures }
                    }
                    TextEdit { width: parent.width; readOnly: true; selectByMouse: true; wrapMode: Text.Wrap; text: modelData.text || ""; color: Theme.text; font: Theme.pageRow }
                }
            }
        }
    }
}
