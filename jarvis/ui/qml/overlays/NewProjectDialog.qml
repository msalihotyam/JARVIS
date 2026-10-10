import QtQuick
import QtQuick.Controls.Basic
import "../theme"
import "../components"

// Gate 6G P1: the new-project popup, on the shared dialog shell. A name
// (required) and an optional "what do you want to do?" text, which becomes
// the project's instruction - the existing field: the store's 2,000-character
// cap and PUBLIC / INTERNAL (6B: nothing preselected, asked only when there is
// text). No folder yet (6H): a project without one is always valid.
// It composes the existing paths: createProject, then - only when exactly one
// new project with the typed name appeared and Core refused nothing - the
// instruction save for that id. Anything else shows Core's notice and saves
// nothing (fail closed). On success the new project opens.
JarvisDialog {
    id: dialog
    objectName: "newProjectDialog"
    titleName: "newProjectTitle"
    title: qsTr("New project")
    preferredWidth: 560
    initialFocus: nameField
    confirmItem: createButton
    enterConfirms: true                     // Enter in the name creates; the instruction's Enter is a new line
    enterGuard: [instructionField]
    readonly property string name: nameField.text.trim()
    readonly property string instruction: instructionField.text
    readonly property bool hasInstruction: instruction.trim() !== ""
    readonly property bool overCap: instruction.length > bridge.projectInstructionCap
    property string classification: ""
    // The create in flight: what was sent and the project ids before it.
    property bool waiting: false
    property var before: []
    property var sent: ({})
    property string failure: ""
    confirmEnabled: name !== "" && !overCap && (!hasInstruction || classification !== "") && !waiting

    function openDialog() {
        bridge.showProject("")              // the grid, and no notice left from an earlier request
        nameField.text = ""; instructionField.text = ""
        classification = ""; failure = ""; before = []; sent = {}; waiting = false
        open()
    }
    function create() {
        if (!confirmEnabled) return
        failure = ""
        before = bridge.projectRecords.map(function (p) { return p.id })
        sent = { name: name, text: instruction, classification: classification }
        waiting = true
        bridge.createProject(name)          // sync or on the worker: settle() reads the answer
    }
    // Every project change while a create is in flight: wait for Core's answer.
    function settle() {
        if (!waiting) return
        var known = before
        var fresh = bridge.projectRecords.filter(function (p) { return known.indexOf(p.id) < 0 })
        if (fresh.length === 0 && bridge.projectNotice === "") return       // not answered yet
        waiting = false
        if (fresh.length !== 1 || fresh[0].name !== sent.name || bridge.projectNotice !== "") {
            failure = bridge.projectNotice !== "" ? bridge.projectNotice
                    : qsTr("The new project could not be identified, so no instruction was saved. Open it from the Projects page to add one.")
            return
        }
        var id = fresh[0].id, what = sent
        close()
        bridge.showProject(id)
        if (what.text.trim() !== "") bridge.saveProjectInstruction(id, what.text, what.classification)
    }
    Connections { target: bridge; function onProjectsChanged() { dialog.settle() } }

    Text { text: qsTr("Project name"); color: Theme.textLabel; font: Theme.pageMeta }
    TextField {
        id: nameField
        objectName: "newProjectName"
        width: parent ? parent.width : 0; height: 36
        selectByMouse: true; color: Theme.text; font: Theme.body; maximumLength: 120
        placeholderText: qsTr("e.g. Kitchen renovation"); placeholderTextColor: Theme.textDim
        background: Rectangle { radius: 6; color: Theme.surfaceDeep; border.width: 1
                                border.color: nameField.activeFocus ? Theme.commandEdge : Theme.border }
    }
    Row {
        spacing: 8
        Text { anchors.verticalCenter: parent.verticalCenter; text: qsTr("What do you want to do?"); color: Theme.textLabel; font: Theme.pageMeta }
        Text { anchors.verticalCenter: parent.verticalCenter; text: qsTr("optional"); color: Theme.textDim; font: Theme.caption }
    }
    Rectangle {
        width: parent ? parent.width : 0; height: 120; radius: 6
        color: Theme.surfaceDeep; border.width: 1
        border.color: dialog.overCap ? Theme.warning : instructionField.activeFocus ? Theme.commandEdge : Theme.border
        ScrollView {
            anchors.fill: parent; anchors.margins: 2
            TextArea {
                id: instructionField
                objectName: "newProjectInstruction"
                wrapMode: TextEdit.Wrap; textFormat: TextEdit.PlainText; selectByMouse: true
                color: Theme.text; font: Theme.body; placeholderTextColor: Theme.textDim
                placeholderText: qsTr("Goal, scope, what to watch for… This text becomes the project's instruction.")
                background: null
            }
        }
    }
    Item {
        width: parent ? parent.width : 0; height: 28
        Row {
            objectName: "newProjectClassRow"
            visible: dialog.hasInstruction
            anchors.verticalCenter: parent.verticalCenter; spacing: 8
            Text { anchors.verticalCenter: parent.verticalCenter; text: qsTr("Classification:"); color: Theme.textLabel; font: Theme.pageMeta }
            // Domain codes, never translated (as the instruction editor shows them).
            Segmented { segmentPrefix: "newProjectClass_"; implicitHeight: 28
                        options: [{ value: "public", label: "PUBLIC" }, { value: "internal", label: "INTERNAL" }]
                        value: dialog.classification; onPicked: function (value) { dialog.classification = value } }
        }
        Text { objectName: "newProjectCounter"; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
               text: qsTr("%1 / %2").arg(instructionField.length).arg(bridge.projectInstructionCap)
               color: dialog.overCap ? Theme.warning : Theme.textMuted; font: Theme.captionFigures }
    }
    Text { objectName: "newProjectReason"; width: parent ? parent.width : 0; wrapMode: Text.Wrap; visible: text !== ""
           color: dialog.overCap ? Theme.warning : Theme.textMuted; font: Theme.caption
           text: dialog.overCap ? qsTr("The instruction can be at most %1 characters.").arg(bridge.projectInstructionCap)
                 : dialog.hasInstruction && dialog.classification === "" ? qsTr("Choose PUBLIC or INTERNAL for the instruction.") : "" }
    Text { objectName: "newProjectNotice"; width: parent ? parent.width : 0; wrapMode: Text.Wrap; visible: text !== ""
           textFormat: Text.PlainText; text: dialog.failure; color: Theme.warning; font: Theme.caption }

    buttons: [
        ActionButton { objectName: "newProjectCancel"; label: qsTr("Cancel"); height: 36; width: 110; onClicked: dialog.close() },
        ActionButton { id: createButton; objectName: "newProjectCreate"; label: qsTr("Create project"); primary: true; height: 36
                       onClicked: dialog.create() }
    ]
}
