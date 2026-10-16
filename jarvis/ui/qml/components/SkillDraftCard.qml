import QtQuick
import "../theme"

// V2.2 S1: one save-as-skill draft (or a new-version proposal) after a task.
// A pure view: output only through the signals. Nothing is saved unless the
// person presses Save (S5); several drafts from one task are several cards.
// Scope (D8/D15): a draft from a project starts in that project and may only
// stay there or become global; a global draft stays global - no picker.
Rectangle {
    id: card
    property string draftName: ""
    property string summary: ""                // saved and used for matching, so shown in full
    property var steps: []
    property var inputs: []
    property var checks: []
    property string projectName: ""           // the draft's own project, "" for a global draft
    property string scope: projectName !== "" ? "project" : "global"
    property bool proposal: false              // a new version of an existing skill
    property int version: 0                    // the proposed version (N+1)
    property string targetName: ""             // b8: the skill a new version replaces, by name
    property string adaptedFrom: ""            // b8: the skill an adaptation came from, by name
    readonly property string chosenScope: projectName !== "" && scope === "project" ? "project" : "global"
    signal approve(string scope)
    signal edit()
    signal reject()
    signal suppress()

    implicitWidth: 560
    implicitHeight: body.height + 28
    radius: Theme.radiusSmall
    color: Theme.surfaceRaised
    border.width: 1; border.color: Theme.alpha(Theme.accent, 0.5)

    component Line: Text {
        width: parent ? parent.width : 0; wrapMode: Text.Wrap; textFormat: Text.PlainText; color: Theme.text; font: Theme.caption
    }
    component Section: Column {
        id: section
        property string heading: ""
        property var items: []
        property bool numbered: false
        property string itemName: ""
        width: parent ? parent.width : 0; spacing: 3
        visible: items && items.length > 0
        Text { text: i18n.upper(section.heading); color: Theme.textLabel; font: Theme.label }
        Repeater {
            model: section.items
            Line { objectName: section.itemName; text: (section.numbered ? (index + 1) + ". " : "· ") + modelData }
        }
    }

    Column {
        id: body
        x: 14; y: 14; width: card.width - 28; spacing: 10
        Text { objectName: "skillDraftQuestion"; width: parent.width; wrapMode: Text.Wrap; textFormat: Text.PlainText
               color: Theme.accent; font: Theme.captionStrong
               text: card.proposal ? qsTr("Version %1 proposal for \"%2\"").arg(card.version).arg(card.draftName)
                                   : qsTr("Save this as a skill?") }
        Text { objectName: "skillDraftName"; width: parent.width; wrapMode: Text.Wrap; textFormat: Text.PlainText
               text: card.draftName; color: Theme.text; font: Theme.pageRowStrong }
        Line { objectName: "skillDraftSummary"; visible: text !== ""; text: card.summary }
        Section { heading: qsTr("Steps"); items: card.steps; numbered: true; itemName: "skillDraftStep" }
        Section { heading: qsTr("Inputs"); items: card.inputs; itemName: "skillDraftInput" }
        Section { heading: qsTr("Checks"); items: card.checks; itemName: "skillDraftCheck" }
        Row {
            spacing: 10
            Text { anchors.verticalCenter: parent.verticalCenter; text: i18n.upper(qsTr("Scope")); color: Theme.textLabel; font: Theme.label }
            Segmented { objectName: "skillDraftScope"; visible: card.projectName !== ""; segmentPrefix: "skillDraftScope_"
                        value: card.chosenScope
                        options: [{ value: "project", label: qsTr("This project (%1)").arg(card.projectName) },
                                  { value: "global", label: qsTr("General") }]
                        onPicked: function (value) { card.scope = value } }
            Text { objectName: "skillDraftScopeGlobal"; visible: card.projectName === ""; anchors.verticalCenter: parent.verticalCenter
                   text: qsTr("General"); color: Theme.text; font: Theme.caption }
        }
        Flow {
            width: parent.width; spacing: 8
            ActionButton { objectName: "skillDraftSave"; primary: true; compact: true; onClicked: card.approve(card.chosenScope)
                           label: card.targetName !== "" ? qsTr("Save as a new version of %1").arg(card.targetName)
                                  : card.adaptedFrom !== "" ? qsTr("Save as an adaptation of %1").arg(card.adaptedFrom) : qsTr("Save") }
            ActionButton { objectName: "skillDraftEdit"; compact: true; label: qsTr("Edit"); onClicked: card.edit() }
            ActionButton { objectName: "skillDraftReject"; compact: true; label: qsTr("Dismiss"); onClicked: card.reject() }
            ActionButton { objectName: "skillDraftSuppress"; compact: true; label: qsTr("Don't ask for tasks like this"); onClicked: card.suppress() }
        }
    }
}
