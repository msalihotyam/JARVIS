import QtQuick
import QtQuick.Controls.Basic
import "../theme"
import "../components"

// V2.2 S1: the saved skills. A pure view - input only through `skills` and
// `skillsEnabled`, output only through the signals below; the caller (SKBRIDGE)
// does every change. A skill is a procedure, never an authority: nothing here
// approves, trusts or pre-approves anything, and every name and step is shown
// as plain text data.
// A skill: { id, name, scope: "global" | "project", project_name, version,
//   versions: [n...], status: "active" | "disabled" | "stale", uses,
//   last_success, failures, steps: [text], inputs: [text], checks: [text],
//   origin: "taught_from_run" | "adapted" | "user_edit" | "version_after_failure",
//   parent_name, parent_version }
SecondaryPage {
    id: page
    title: qsTr("Skills")
    property var skills: []
    property bool skillsEnabled: false
    property string selectedId: ""
    signal editRequested(string id)
    signal enabledToggled(string id, bool on)
    signal deleteRequested(string id)
    signal rollbackRequested(string id, int version)
    signal enableSkillsRequested()

    readonly property var selected: {
        for (var i = 0; i < skills.length; ++i) if (skills[i].id === selectedId) return skills[i]
        return skills.length > 0 ? skills[0] : null
    }
    function scopeWord(s) { return s.scope === "project" ? qsTr("Project") : qsTr("Global") }
    function statusWord(status) {
        return status === "disabled" ? qsTr("Off") : status === "stale" ? qsTr("Stale") : qsTr("Active")
    }
    function statusTint(status) {
        return status === "disabled" ? Theme.textMuted : status === "stale" ? Theme.warning : Theme.success
    }
    function usage(s) {
        var parts = [s.uses === 1 ? qsTr("1 use") : qsTr("%1 uses").arg(s.uses || 0)]
        if (s.last_success) parts.push(qsTr("last success %1").arg(page.when(s.last_success)))
        if (s.failures) parts.push(s.failures === 1 ? qsTr("1 failure") : qsTr("%1 failures").arg(s.failures))
        return parts.join(" · ")
    }
    function provenance(s) {
        if (!s) return ""
        if (s.origin === "adapted") return qsTr("Adapted from the \"%1\" skill, v%2").arg(s.parent_name || "").arg(s.parent_version || 0)
        if (s.origin === "taught_from_run") return qsTr("Saved from a finished task")
        if (s.origin === "user_edit") return qsTr("Edited by you")
        if (s.origin === "version_after_failure") return qsTr("New version after a failed run")
        return ""
    }

    component Line: Text {
        width: parent ? parent.width : 0; wrapMode: Text.Wrap; textFormat: Text.PlainText; color: Theme.text; font: Theme.pageMeta
    }
    component Section: Column {
        id: section
        property string heading: ""
        property var items: []
        property bool numbered: false
        property string itemName: ""
        width: parent ? parent.width : 0; spacing: 4
        visible: items && items.length > 0
        Text { text: i18n.upper(section.heading); color: Theme.textLabel; font: Theme.pageSection }
        Repeater {
            model: section.items
            Line { objectName: section.itemName; text: (section.numbered ? (index + 1) + ". " : "· ") + modelData }
        }
    }

    Column {
        anchors.fill: parent
        spacing: 12
        // S4: skills off - nothing is matched, announced or offered; saved skills stay listed.
        PageEmpty { objectName: "skillsOff"; width: parent.width; visible: !page.skillsEnabled
                    title: qsTr("Skills are off")
                    note: qsTr("While skills are off JARVIS does not match, announce or offer skills. Saved skills are kept.")
                    action: qsTr("Turn skills on")
                    onActed: page.enableSkillsRequested() }
        PageEmpty { objectName: "skillsEmpty"; width: parent.width; visible: page.skills.length === 0
                    title: qsTr("No saved skills yet.")
                    note: qsTr("When a task finishes, JARVIS can offer to save its steps as a skill. Nothing is saved without your approval.") }
        Row {
            id: split
            width: parent.width; height: parent.height - y; spacing: 16
            visible: page.skills.length > 0
            ListView {
                id: list
                objectName: "skillsList"
                width: Math.round(split.width * 0.45); height: split.height; clip: true; spacing: 8
                model: page.skills
                ScrollBar.vertical: ScrollBar {}
                delegate: ListRow {
                    objectName: "skillRow"
                    width: ListView.view.width
                    title: modelData.name || ""
                    titleName: "skillRowName"
                    badge: page.scopeWord(modelData)
                    meta: "v" + (modelData.version || 0)
                    detail: page.usage(modelData)
                    detailName: "skillRowUsage"
                    current: page.selected !== null && page.selected.id === modelData.id
                    muted: modelData.status === "disabled"
                    onActivated: page.selectedId = modelData.id
                    Pill { objectName: "skillRowStatus"; text: page.statusWord(modelData.status); tint: page.statusTint(modelData.status)
                           font: Theme.pageSection; implicitHeight: 24 }
                }
            }
            Flickable {
                id: detail
                objectName: "skillDetail"
                width: split.width - list.width - split.spacing; height: split.height
                contentHeight: pane.height; clip: true; boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar {}
                Column {
                    id: pane
                    width: detail.width; spacing: 12
                    visible: page.selected !== null
                    readonly property var s: page.selected || ({})
                    Text { objectName: "skillDetailName"; width: parent.width; wrapMode: Text.Wrap; textFormat: Text.PlainText
                           text: pane.s.name || ""; color: Theme.text; font: Theme.subtitle }
                    Line { objectName: "skillDetailMeta"; color: Theme.textLabel
                           text: (pane.s.scope === "project" && pane.s.project_name ? pane.s.project_name : page.scopeWord(pane.s))
                                 + " · v" + (pane.s.version || 0) + " · " + page.statusWord(pane.s.status) }
                    Line { objectName: "skillProvenance"; visible: text !== ""; color: Theme.textLabel; text: page.provenance(page.selected) }
                    // K3: stale is said in words, and what it means.
                    Line { objectName: "skillStaleNote"; visible: pane.s.status === "stale"; color: Theme.warning
                           text: qsTr("This skill failed in recent runs. It is not used on its own until a run you approve succeeds with it again.") }
                    Section { heading: qsTr("Steps"); items: pane.s.steps || []; numbered: true; itemName: "skillStep" }
                    Section { heading: qsTr("Inputs"); items: pane.s.inputs || []; itemName: "skillInput" }
                    Section { heading: qsTr("Checks"); items: pane.s.checks || []; itemName: "skillCheck" }
                    Row {
                        spacing: 8
                        ActionButton { objectName: "skillEdit"; label: qsTr("Edit"); onClicked: page.editRequested(pane.s.id) }
                        ActionButton { objectName: "skillToggle"; label: pane.s.status === "disabled" ? qsTr("Turn on") : qsTr("Turn off")
                                       onClicked: page.enabledToggled(pane.s.id, pane.s.status === "disabled") }
                        ActionButton { id: deleteButton; objectName: "skillDelete"; danger: true; label: qsTr("Delete")
                                       onClicked: { deleteDialog.targetId = pane.s.id; deleteDialog.targetName = pane.s.name || ""; deleteDialog.openFrom(deleteButton) } }
                    }
                    // S6: an earlier version comes back by an event; no version is ever removed.
                    Column {
                        width: parent.width; spacing: 6
                        readonly property var earlier: (pane.s.versions || []).filter(function (v) { return v !== pane.s.version })
                        visible: earlier.length > 0
                        Text { text: i18n.upper(qsTr("Versions")); color: Theme.textLabel; font: Theme.pageSection }
                        Repeater {
                            model: parent.earlier
                            Row {
                                spacing: 10
                                Text { anchors.verticalCenter: parent.verticalCenter; text: "v" + modelData; color: Theme.text; font: Theme.pageMetaFigures }
                                ActionButton { objectName: "skillRollback"; compact: true; label: qsTr("Return to this version")
                                               onClicked: page.rollbackRequested(pane.s.id, modelData) }
                            }
                        }
                    }
                }
            }
        }
    }

    // D4: delete is a tombstone - asked first, focus on the safe button.
    JarvisDialog {
        id: deleteDialog
        objectName: "skillDeleteDialog"
        danger: true
        label: qsTr("Delete skill")
        title: qsTr("Delete the skill \"%1\"?").arg(targetName)
        titleName: "skillDeleteTitle"
        initialFocus: cancelButton
        confirmItem: confirmButton
        property string targetId: ""
        property string targetName: ""
        Line { text: qsTr("The skill is no longer shown or used, and it cannot be brought back. Its past runs stay in the history. To pause it instead, turn it off.") }
        buttons: [
            ActionButton { id: cancelButton; objectName: "skillDeleteCancel"; label: qsTr("Cancel"); onClicked: deleteDialog.close() },
            ActionButton { id: confirmButton; objectName: "skillDeleteConfirm"; danger: true; primary: true; label: qsTr("Delete")
                           onClicked: { page.deleteRequested(deleteDialog.targetId); deleteDialog.close() } }
        ]
    }
}
