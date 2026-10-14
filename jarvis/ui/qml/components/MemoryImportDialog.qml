import QtQuick
import QtQuick.Dialogs
import "../theme"

// V2.1b (IMPUI): import an earlier chat history into project memory.
// Pick an export file -> the app's preview groups it, each group with a
// proposed project -> the person confirms a project per group (a group left
// unconfirmed is not sent) -> the app extracts. Every imported memory lands
// PENDING in the approval list; the imported text is data, never executed.
// Core decides every refusal; this dialog shows the answer as returned.
JarvisDialog {
    id: importDialog
    property string planId: ""
    property var groups: []
    property var chosen: ({})                 // group_id -> project_id, set only by the person's click
    property bool running: false
    property string notice: ""                // the last refusal, as returned
    property string result: ""
    readonly property int confirmedCount: Object.keys(chosen).length
    readonly property var projects: bridge.projectRecords.filter(function (p) { return p.active })
    label: qsTr("Import history")
    preferredWidth: 720
    onClosed: if (!running) { planId = ""; groups = []; chosen = ({}); notice = ""; result = "" }

    function projectName(id) { const p = projects.find(function (x) { return x.id === id }); return p ? p.name : "" }
    function choose(groupId, projectId) {
        const next = Object.assign({}, chosen)
        if (next[groupId] === projectId) delete next[groupId]; else next[groupId] = projectId
        chosen = next
    }

    Connections {
        target: bridge
        function onImportPreviewDone(answer) {
            importDialog.chosen = ({}); importDialog.result = ""
            importDialog.planId = answer.ok === true ? String(answer.plan_id || "") : ""
            importDialog.groups = answer.ok === true ? (answer.groups || []) : []
            importDialog.notice = answer.ok === true ? "" : String(answer.error || "")
        }
        function onImportRunDone(answer) {
            if (answer.plan_id !== importDialog.planId) return
            importDialog.running = false
            importDialog.notice = answer.ok === true ? "" : String(answer.error || "")
            if (answer.ok !== true) return
            importDialog.result = (answer.cancelled ? qsTr("Import stopped. ") : "")
                + qsTr("Memory records waiting for your approval in the Memory list: %1").arg(answer.pending || 0)
            importDialog.chosen = ({})
        }
        function onImportCancelDone(answer) {
            if (answer.plan_id === importDialog.planId && answer.ok !== true) importDialog.notice = String(answer.error || "")
        }
    }

    Text { objectName: "memoryImportSafety"; width: parent ? parent.width : 0; wrapMode: Text.Wrap; color: Theme.textLabel; font: Theme.pageRow
           text: qsTr("Imported conversations are read as data only. Nothing in them is ever executed or followed as an instruction, and every memory found waits for your approval.") }
    ActionButton { objectName: "memoryImportPick"; label: qsTr("Choose export file"); height: 36; enabled: !importDialog.running
                   onClicked: filePicker.open() }
    Text { objectName: "memoryImportNotice"; width: parent ? parent.width : 0; wrapMode: Text.Wrap; visible: text !== ""
           textFormat: Text.PlainText; text: importDialog.notice; color: Theme.warning; font: Theme.pageMeta }
    Text { objectName: "memoryImportResult"; width: parent ? parent.width : 0; wrapMode: Text.Wrap; visible: text !== ""
           text: importDialog.result; color: Theme.success; font: Theme.pageRow }
    Text { objectName: "memoryImportHint"; visible: importDialog.groups.length > 0; width: parent ? parent.width : 0; wrapMode: Text.Wrap
           text: qsTr("Confirm a project for each conversation you want imported. A conversation without a confirmed project is skipped.")
           color: Theme.textMuted; font: Theme.pageMeta }
    Repeater {
        model: importDialog.groups
        delegate: Rectangle {
            id: card
            objectName: "memoryImportGroup"
            required property var modelData
            readonly property string groupId: String(modelData.group_id)
            width: parent ? parent.width : 0; height: cardColumn.height + 20; radius: Theme.radiusSmall
            color: Theme.glassNested; border.width: 1
            border.color: importDialog.chosen[groupId] !== undefined ? Theme.accent : Theme.alpha(Theme.text, 0.10)
            Column {
                id: cardColumn
                x: 10; y: 10; width: parent.width - 20; spacing: 6
                // The title is the export's own text: plain data, never markup.
                Text { objectName: "memoryImportGroupTitle"; width: parent.width; elide: Text.ElideRight; textFormat: Text.PlainText
                       text: card.modelData.title || qsTr("Untitled conversation"); color: Theme.text; font: Theme.pageRow }
                Text { objectName: "memoryImportGroupMeta"; color: Theme.textMuted; font: Theme.pageMeta
                       text: qsTr("Messages: %1").arg(card.modelData.items || 0)
                             + (card.modelData.proposed_project_id ? " · " + qsTr("Suggested project: %1").arg(importDialog.projectName(card.modelData.proposed_project_id)) : "") }
                Flow {
                    width: parent.width; spacing: 6
                    Repeater {
                        model: importDialog.projects
                        ActionButton { objectName: "memoryImportScope"; height: 30; compact: true; enabled: !importDialog.running
                                       label: modelData.name; primary: importDialog.chosen[card.groupId] === modelData.id
                                       onClicked: importDialog.choose(card.groupId, modelData.id) }
                    }
                }
            }
        }
    }

    buttons: [
        ActionButton { objectName: "memoryImportClose"; label: qsTr("Close"); height: 36; onClicked: importDialog.close() },
        ActionButton { objectName: "memoryImportStop"; label: qsTr("Stop import"); height: 36; visible: importDialog.running
                       onClicked: bridge.cancelImport(importDialog.planId) },
        ActionButton { objectName: "memoryImportRun"; primary: true; height: 36
                       label: qsTr("Import (%1)").arg(importDialog.confirmedCount)
                       enabled: importDialog.planId !== "" && importDialog.confirmedCount > 0 && !importDialog.running
                       onClicked: { importDialog.running = true; importDialog.result = ""; importDialog.notice = ""
                                    bridge.importHistoryRun(importDialog.planId, importDialog.chosen) } }
    ]

    FileDialog {
        id: filePicker
        objectName: "memoryImportFileDialog"
        fileMode: FileDialog.OpenFile
        nameFilters: [qsTr("Chat exports (%1)").arg("*.json *.md *.txt")]
        onAccepted: { importDialog.notice = ""; bridge.importHistoryPreview(selectedFile) }
    }
}
