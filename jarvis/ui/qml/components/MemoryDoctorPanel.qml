import QtQuick
import "../theme"

// V2.1b: the Memory Doctor's report for the shown project, read-only. Findings
// are listed worst first in words; 'Repair' appears only where the Doctor
// proposes one, asks for confirmation, and runs one repair at a time. The
// app re-diagnoses and decides (I1/I2); nothing is deleted (I8).
Column {
    id: panel
    property string scope: ""
    property var report: ({})
    property string busy: ""            // the finding whose repair is in flight
    property var asked: ({})            // the finding the confirmation is open for
    // Worst first, the Doctor's own order within a severity (a stable grouping, not sort()).
    readonly property var findings: [0, 1, 2, -1].reduce(function(out, r) {
        return out.concat((report.findings || []).filter(function(f) { return panel.rank(f) === r })) }, [])
    width: parent ? parent.width : 0
    spacing: 8
    visible: scope !== ""
    function rank(f) { return ["error", "warning", "info"].indexOf(f.severity) }
    function count(codes) { return findings.filter(function(f) { return codes.indexOf(f.code) >= 0 }).length }
    function load() { if (scope !== "") bridge.memoryDoctor(scope) }
    onScopeChanged: { report = ({}); load() }
    Component.onCompleted: load()

    function severityWord(s) { return s === "error" ? qsTr("Error") : s === "warning" ? qsTr("Warning") : s === "info" ? qsTr("Info") : s }
    function summary(f) {
        switch (f.summary_key) {
        case "memory_doctor.orphan_scope": return qsTr("A record belongs to a project or conversation that no longer exists.")
        case "memory_doctor.stale.expired": return qsTr("A record is past its expiry date but still in use.")
        case "memory_doctor.stale.unverified": return qsTr("A record has not been verified for a long time.")
        case "memory_doctor.conflict_unresolved": return qsTr("Approved records on the same subject contradict each other.")
        case "memory_doctor.supersession_cycle": return qsTr("Records replace each other in a loop.")
        case "memory_doctor.dangling_supersession": return qsTr("A record replaces a record that does not exist.")
        case "memory_doctor.fts_drift": return qsTr("The memory search index is out of step with the records.")
        case "memory_doctor.pending_too_old": return qsTr("A record has been waiting for your approval for over two weeks.")
        case "memory_doctor.filter_violation": return qsTr("A record holds content that must never be stored.")
        case "memory_doctor.legacy_m1_unmapped": return qsTr("An item from the old memory format has no new equivalent.")
        case "memory_doctor.lesson_stale": return qsTr("A lesson has not been confirmed for over 90 days.")
        case "memory_doctor.lesson_contradicted": return qsTr("A newer record contradicts a lesson.")
        }
        return f.code || ""
    }
    function actionWord(a) {
        return a === "retract" ? qsTr("Retract the record") : a === "expire" ? qsTr("Mark the record expired")
             : a === "supersede" ? qsTr("Replace the older record with the newer one") : a === "rebuild_fts" ? qsTr("Rebuild the search index") : a
    }

    Connections {
        target: bridge
        function onMemoryDoctorDone(answer) { if (answer.project_id === panel.scope) panel.report = answer }
        function onMemoryRepairDone(answer) { if (answer.finding_id === panel.busy) { panel.busy = ""; panel.load() } }
    }

    Text { text: qsTr("MEMORY HEALTH"); color: Theme.textLabel; font: Theme.pageSection }
    Text { objectName: "memoryDoctorCounts"; visible: panel.report.ok === true
           text: qsTr("Stale: %1 · Conflicts: %2 · Orphaned: %3")
                 .arg(panel.count(["STALE", "LESSON_STALE"])).arg(panel.count(["CONFLICT_UNRESOLVED", "LESSON_CONTRADICTED"])).arg(panel.count(["ORPHAN_SCOPE"]))
           color: Theme.textMuted; font: Theme.pageMeta }
    Text { objectName: "memoryDoctorHealthy"; visible: panel.report.ok === true && panel.findings.length === 0; width: parent.width; wrapMode: Text.Wrap
           text: qsTr("Memory is healthy: the Doctor found nothing to fix."); color: Theme.success; font: Theme.pageMeta }
    Text { objectName: "memoryDoctorError"; visible: panel.report.ok === false; width: parent.width; wrapMode: Text.Wrap; textFormat: Text.PlainText
           text: qsTr("The health check could not run: %1").arg(panel.report.error || ""); color: Theme.warning; font: Theme.pageMeta }
    Repeater {
        model: panel.findings
        Rectangle {
            id: card
            objectName: "memoryDoctorFinding"
            readonly property color tone: modelData.severity === "error" ? Theme.danger : modelData.severity === "warning" ? Theme.warning : Theme.textMuted
            width: panel.width; height: row.height + 20; radius: Theme.radiusSmall
            color: Theme.surfaceRaised; border.width: 1; border.color: tone
            Row { id: row; x: 14; y: 10; width: parent.width - 28; spacing: 12
                Text { objectName: "memoryDoctorSeverity"; width: 72; anchors.verticalCenter: parent.verticalCenter
                       text: i18n.upper(panel.severityWord(modelData.severity)); color: card.tone; font: Theme.pageSection }
                Text { objectName: "memoryDoctorSummary"; anchors.verticalCenter: parent.verticalCenter; wrapMode: Text.Wrap; textFormat: Text.PlainText
                       width: parent.width - 84 - (repair.visible ? repair.width + 12 : 0)
                       text: panel.summary(modelData); color: Theme.text; font: Theme.pageRow }
                ActionButton { id: repair; objectName: "memoryDoctorRepair"; anchors.verticalCenter: parent.verticalCenter
                               visible: !!modelData.repair; enabled: panel.busy === ""
                               label: panel.busy === modelData.finding_id ? qsTr("Repairing…") : qsTr("Repair")
                               onClicked: { panel.asked = modelData; confirm.openFrom(repair) } }
            }
        }
    }

    JarvisDialog {
        id: confirm
        objectName: "memoryDoctorConfirm"
        label: qsTr("Memory repair")
        title: qsTr("Apply this repair?")
        initialFocus: cancelButton
        confirmItem: confirmButton
        Text { objectName: "memoryDoctorConfirmText"; width: parent ? parent.width : 0; wrapMode: Text.Wrap; textFormat: Text.PlainText; color: Theme.text; font: Theme.pageRow
               text: panel.summary(panel.asked) + "\n" + qsTr("Repair: %1. Nothing is deleted.").arg(panel.actionWord((panel.asked.repair || {}).action)) }
        buttons: [
            ActionButton { id: cancelButton; objectName: "memoryDoctorCancel"; label: qsTr("Cancel"); height: 36; onClicked: confirm.close() },
            ActionButton { id: confirmButton; objectName: "memoryDoctorApply"; label: qsTr("Repair"); primary: true; height: 36; enabled: panel.busy === ""
                           onClicked: { panel.busy = panel.asked.finding_id; bridge.applyMemoryRepair(panel.busy); confirm.close() } }
        ]
    }
}
