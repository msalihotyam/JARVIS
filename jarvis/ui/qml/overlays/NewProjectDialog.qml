import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Dialogs
import "../theme"
import "../components"

// Gate 6G P1: the new-project popup, on the shared dialog shell. A name
// (required) and an optional "what do you want to do?" text, which becomes
// the project's instruction - the existing field: the store's 2,000-character
// cap and PUBLIC / INTERNAL (6B: nothing preselected, asked only when there is
// text). Gate 6H (G6H2): optionally an EXISTING folder ("Browse…"), its exact
// path shown before the click; a project without one is always valid. No
// folder is created here (G6H1b).
// It composes the existing paths: createProject, then - only when exactly one
// new project with the typed name appeared and Core refused nothing - the
// folder link and the instruction save for that id. Anything else shows
// Core's notice and saves nothing (fail closed). A refused folder leaves the
// project created without one: the reason shows here, another folder can be
// linked or the project kept without one. On success the new project opens.
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
    property string folder: ""              // the exact path the link sends ("" = no folder)
    property string createdId: ""           // created, but its folder was refused
    confirmEnabled: createdId !== "" ? folder !== ""
                                     : name !== "" && !overCap && (!hasInstruction || classification !== "") && !waiting

    function openDialog() {
        bridge.showProject("")              // the grid, and no notice left from an earlier request
        nameField.text = ""; instructionField.text = ""
        classification = ""; failure = ""; before = []; sent = {}; waiting = false
        folder = ""; createdId = ""
        open()
    }
    function create() {
        if (!confirmEnabled) return
        failure = ""
        if (createdId !== "") {             // the project exists: only another folder is tried
            if (bridge.linkProjectFolder(createdId, folder)) finish(createdId, null)
            else failure = refused(bridge.projectFolderNotice)
            return
        }
        before = bridge.projectRecords.map(function (p) { return p.id })
        sent = { name: name, text: instruction, classification: classification, folder: folder }
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
        var id = fresh[0].id, what = sent, b = bridge
        if (what.folder !== "" && !b.linkProjectFolder(id, what.folder)) {
            createdId = id
            failure = refused(b.projectFolderNotice)
            if (what.text.trim() !== "") b.saveProjectInstruction(id, what.text, what.classification)
            return
        }
        finish(id, what)
    }
    function refused(reason) { return qsTr("The project was created, but the folder was not linked: %1").arg(reason) }
    // Opens the project; `what` carries an instruction still to save (null = saved).
    function finish(id, what) {
        var b = bridge
        failure = ""                        // a finished create keeps a waiting new chat's choice
        close()
        b.showProject(id)
        if (what && what.text.trim() !== "") b.saveProjectInstruction(id, what.text, what.classification)
    }
    Connections { target: bridge; function onProjectsChanged() { dialog.settle() } }

    Text { text: qsTr("Project name"); color: Theme.textLabel; font: Theme.pageMeta }
    TextField {
        id: nameField
        objectName: "newProjectName"
        width: parent ? parent.width : 0; height: 36
        selectByMouse: true; color: Theme.text; font: Theme.body; maximumLength: 120
        readOnly: dialog.createdId !== ""
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
                readOnly: dialog.createdId !== ""
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
            Segmented { segmentPrefix: "newProjectClass_"; implicitHeight: 28; enabled: dialog.createdId === ""
                        options: [{ value: "public", label: "PUBLIC" }, { value: "internal", label: "INTERNAL" }]
                        value: dialog.classification; onPicked: function (value) { dialog.classification = value } }
        }
        Text { objectName: "newProjectCounter"; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
               text: qsTr("%1 / %2").arg(instructionField.length).arg(bridge.projectInstructionCap)
               color: dialog.overCap ? Theme.warning : Theme.textMuted; font: Theme.captionFigures }
    }
    // Gate 6H: "Klasöre bağla" - an existing folder, its full path always shown.
    Row {
        spacing: 8
        Text { anchors.verticalCenter: parent.verticalCenter; text: qsTr("Link a folder"); color: Theme.textLabel; font: Theme.pageMeta }
        Text { anchors.verticalCenter: parent.verticalCenter; text: qsTr("optional"); color: Theme.textDim; font: Theme.caption }
    }
    Item {
        width: parent ? parent.width : 0; height: Math.max(36, folderText.implicitHeight)
        ActionButton { id: browse; objectName: "newProjectFolderBrowse"; label: qsTr("Browse…"); height: 36
                       anchors.verticalCenter: parent.verticalCenter; enabled: !dialog.waiting; onClicked: folderPicker.open() }
        Text { id: folderText; objectName: "newProjectFolderPath"; x: browse.width + 10; anchors.verticalCenter: parent.verticalCenter
               width: parent.width - x - (clearFolder.visible ? clearFolder.width + 10 : 0); wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText
               text: dialog.folder || qsTr("No folder. A project can always be created without one.")
               color: dialog.folder ? Theme.text : Theme.textDim; font: Theme.caption }
        ActionButton { id: clearFolder; objectName: "newProjectFolderClear"; compact: true; label: qsTr("Remove")
                       anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                       visible: dialog.folder !== "" && !dialog.waiting; onClicked: dialog.folder = "" }
    }
    Text { objectName: "newProjectFolderNote"; width: parent ? parent.width : 0; wrapMode: Text.Wrap; visible: dialog.folder !== ""
           text: qsTr("JARVIS does not read this folder, write into it or send it to the model: it is context for you only.")
           color: Theme.textMuted; font: Theme.caption }
    FolderDialog {
        id: folderPicker
        objectName: "newProjectFolderPicker"
        onAccepted: dialog.folder = bridge.folderPath(selectedFolder)
    }
    Text { objectName: "newProjectReason"; width: parent ? parent.width : 0; wrapMode: Text.Wrap; visible: text !== ""
           color: dialog.overCap ? Theme.warning : Theme.textMuted; font: Theme.caption
           text: dialog.overCap ? qsTr("The instruction can be at most %1 characters.").arg(bridge.projectInstructionCap)
                 : dialog.hasInstruction && dialog.classification === "" ? qsTr("Choose PUBLIC or INTERNAL for the instruction.") : "" }
    Text { objectName: "newProjectNotice"; width: parent ? parent.width : 0; wrapMode: Text.Wrap; visible: text !== ""
           textFormat: Text.PlainText; text: dialog.failure; color: Theme.warning; font: Theme.caption }

    buttons: [
        ActionButton { objectName: "newProjectCancel"; height: 36; width: dialog.createdId !== "" ? implicitWidth : 110
                       label: dialog.createdId !== "" ? qsTr("Continue without a folder") : qsTr("Cancel")
                       onClicked: dialog.createdId !== "" ? dialog.finish(dialog.createdId, null) : dialog.close() },
        ActionButton { id: createButton; objectName: "newProjectCreate"; primary: true; height: 36
                       label: dialog.createdId !== "" ? qsTr("Link folder") : qsTr("Create project")
                       onClicked: dialog.create() }
    ]
}
