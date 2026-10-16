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
    // SKUIB: lessons { experience_id, kind, text, state: "active" | "disabled" }, the
    // open lesson, active suppressions { key, scope: "task" | "global" }, the count of
    // unreadable records, and the last lesson/undo refusal - shown verbatim.
    property var lessons: []
    property var lessonDetail: ({})
    property var suppressions: []
    property int unreadable: 0
    property string notice: ""
    // SKBRIDGE: declared for the caller, but no editor exists, so no control sends it.
    signal editRequested(string id)
    signal enabledToggled(string id, bool on)
    signal deleteRequested(string id)
    signal rollbackRequested(string id, int version)
    signal enableSkillsRequested()
    // b4: a request to Core to run the skill - every step still goes through policy and approval.
    signal runRequested(string id)
    signal lessonViewRequested(string id)
    signal lessonDisableRequested(string id)
    signal lessonDeleteRequested(string id)
    signal unsuppressRequested(string key, string scope)

    readonly property var selected: {
        for (var i = 0; i < skills.length; ++i) if (skills[i].id === selectedId) return skills[i]
        return skills.length > 0 ? skills[0] : null
    }
    function scopeWord(s) { return s.scope === "project" ? qsTr("Project") : qsTr("General") }
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
        Line { objectName: "skillsUnreadable"; visible: page.unreadable > 0; color: Theme.warning
               text: qsTr("%1 records could not be read").arg(page.unreadable) }
        Line { objectName: "skillsNotice"; visible: page.notice !== ""; color: Theme.warning; text: page.notice }
        Row {
            id: split
            width: parent.width; height: parent.height - y - extras.height - parent.spacing; spacing: 16
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
                        ActionButton { objectName: "skillRun"; primary: true; label: qsTr("Run")
                                       visible: page.skillsEnabled && pane.s.status === "active"
                                       onClicked: page.runRequested(pane.s.id) }
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
        // b1/b7: lessons and suppressions; their own scroll area under the split.
        Flickable {
            id: extras
            objectName: "skillsExtras"
            width: parent.width
            height: page.skills.length > 0 ? Math.min(extrasColumn.height, Math.round(parent.height * 0.4))
                                           : Math.max(0, parent.height - split.y)
            contentHeight: extrasColumn.height; clip: true; boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar {}
            Column {
                id: extrasColumn
                width: extras.width; spacing: 8
                Text { text: i18n.upper(qsTr("Lessons")); color: Theme.textLabel; font: Theme.pageSection }
                Line { objectName: "lessonsEmpty"; visible: page.lessons.length === 0; color: Theme.textLabel; text: qsTr("No lessons yet.") }
                Repeater {
                    model: page.lessons
                    Row {
                        objectName: "lessonRow"
                        width: extrasColumn.width; spacing: 8
                        Pill { objectName: "lessonState"; anchors.verticalCenter: parent.verticalCenter
                               text: modelData.state === "disabled" ? qsTr("Off") : qsTr("Active")
                               tint: modelData.state === "disabled" ? Theme.textMuted : Theme.success
                               font: Theme.pageSection; implicitHeight: 24 }
                        Text { objectName: "lessonText"; anchors.verticalCenter: parent.verticalCenter
                               width: Math.max(0, parent.width - x - lessonButtons.width - parent.spacing)
                               elide: Text.ElideRight; textFormat: Text.PlainText; color: Theme.text; font: Theme.pageMeta
                               text: modelData.text || "" }
                        Row {
                            id: lessonButtons
                            spacing: 6
                            ActionButton { objectName: "lessonView"; compact: true; label: qsTr("View")
                                           onClicked: page.lessonViewRequested(modelData.experience_id) }
                            ActionButton { id: lessonDisableButton; objectName: "lessonDisable"; compact: true; label: qsTr("Turn off")
                                           visible: modelData.state !== "disabled"
                                           onClicked: { lessonDisableDialog.targetId = modelData.experience_id; lessonDisableDialog.openFrom(lessonDisableButton) } }
                            ActionButton { id: lessonDeleteButton; objectName: "lessonDelete"; compact: true; danger: true; label: qsTr("Delete")
                                           onClicked: { lessonDeleteDialog.targetId = modelData.experience_id; lessonDeleteDialog.openFrom(lessonDeleteButton) } }
                        }
                    }
                }
                // The lesson asked for with "View": its text and when it was learned, as plain data.
                Line { objectName: "lessonDetail"; visible: !!page.lessonDetail.experience_id
                       text: (page.lessonDetail.text || "") + (page.lessonDetail.created_at ? " · " + page.when(page.lessonDetail.created_at) : "") }
                Text { text: i18n.upper(qsTr("Not offered")); color: Theme.textLabel; font: Theme.pageSection }
                Line { objectName: "suppressionsEmpty"; visible: page.suppressions.length === 0; color: Theme.textLabel
                       text: qsTr("Nothing is held back from offers.") }
                Repeater {
                    model: page.suppressions
                    Row {
                        objectName: "suppressionRow"
                        spacing: 10
                        Text { objectName: "suppressionKey"; anchors.verticalCenter: parent.verticalCenter
                               textFormat: Text.PlainText; color: Theme.text; font: Theme.pageMeta
                               text: modelData.scope === "global" ? qsTr("All skill offers") : (modelData.key || "") }
                        ActionButton { objectName: "suppressionUndo"; compact: true; label: qsTr("Undo")
                                       onClicked: page.unsuppressRequested(modelData.key || "", modelData.scope || "task") }
                    }
                }
            }
        }
    }

    // b1: no "turn back on" in this version - said before the click.
    JarvisDialog {
        id: lessonDisableDialog
        objectName: "lessonDisableDialog"
        label: qsTr("Turn off lesson")
        title: qsTr("Turn this lesson off?")
        titleName: "lessonDisableTitle"
        initialFocus: lessonDisableCancel
        confirmItem: lessonDisableConfirm
        property string targetId: ""
        Line { text: qsTr("JARVIS stops using this lesson. This cannot be undone in this version.") }
        buttons: [
            ActionButton { id: lessonDisableCancel; objectName: "lessonDisableCancel"; label: qsTr("Cancel"); onClicked: lessonDisableDialog.close() },
            ActionButton { id: lessonDisableConfirm; objectName: "lessonDisableConfirm"; primary: true; label: qsTr("Turn off")
                           onClicked: { page.lessonDisableRequested(lessonDisableDialog.targetId); lessonDisableDialog.close() } }
        ]
    }
    // b1: a delete is final.
    JarvisDialog {
        id: lessonDeleteDialog
        objectName: "lessonDeleteDialog"
        danger: true
        label: qsTr("Delete lesson")
        title: qsTr("Delete this lesson?")
        titleName: "lessonDeleteTitle"
        initialFocus: lessonDeleteCancel
        confirmItem: lessonDeleteConfirm
        property string targetId: ""
        Line { text: qsTr("The lesson is deleted for good and cannot be brought back.") }
        buttons: [
            ActionButton { id: lessonDeleteCancel; objectName: "lessonDeleteCancel"; label: qsTr("Cancel"); onClicked: lessonDeleteDialog.close() },
            ActionButton { id: lessonDeleteConfirm; objectName: "lessonDeleteConfirm"; danger: true; primary: true; label: qsTr("Delete")
                           onClicked: { page.lessonDeleteRequested(lessonDeleteDialog.targetId); lessonDeleteDialog.close() } }
        ]
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
