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
// path shown before the click; a project without one is always valid.
// G6H1b (K1): or "Create a folder" - on the Desktop or in Documents\JARVIS
// Projeleri. Core computes the path (a preview per name and checkbox, an
// older answer discarded) and the click on that exact shown path is the
// approval: ONE bridge call creates the project, then the folder for the id
// from that create's own answer. If the project was created and its folder
// was not, the popup ends for THAT project: the reason and only "OK".
// Without a folder to create, it composes the existing paths: createProject, then - only when exactly one
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
    // K1 "Create a folder": the preview Core gave for exactly these inputs.
    property bool makeFolder: false
    property bool onDesktop: false
    property int previewGeneration: 0
    property string previewAsked: ""        // the inputs the newest request carried
    property string previewKey: ""          // the inputs previewPath / previewNotice answer
    property string previewPath: ""
    property string previewNotice: ""
    property string finalId: ""             // created, its folder was not: only "OK" is left
    readonly property string inputKey: name + "\n" + onDesktop
    readonly property bool previewReady: makeFolder && previewPath !== "" && previewKey === inputKey
    readonly property bool locked: createdId !== "" || finalId !== ""
    // Open until Core's answer, and in the final state until "OK" opens that project.
    closePolicy: waiting || finalId !== "" ? Popup.NoAutoClose : Popup.CloseOnEscape | Popup.CloseOnPressOutside
    confirmEnabled: finalId !== "" ? true
                  : createdId !== "" ? folder !== ""
                  : name !== "" && !overCap && (!hasInstruction || classification !== "") && !waiting
                    && (!makeFolder || previewReady)

    function openDialog() {
        bridge.showProject("")              // the grid, and no notice left from an earlier request
        nameField.text = ""; instructionField.text = ""
        classification = ""; failure = ""; before = []; sent = {}; waiting = false
        folder = ""; createdId = ""; finalId = ""; makeFolder = false; onDesktop = false
        requestPreview()                    // a new generation: an answer still on its way is dropped
        open()
    }
    function requestPreview() {
        previewGeneration += 1
        previewPath = ""; previewNotice = ""; previewKey = ""; previewAsked = inputKey
        if (makeFolder && name !== "" && !locked) bridge.previewProjectFolder(name, onDesktop, previewGeneration)
    }
    onInputKeyChanged: if (makeFolder) requestPreview()
    onMakeFolderChanged: requestPreview()
    function previewed(generation, path, notice) {
        if (generation !== previewGeneration) return                    // an answer for older inputs
        previewPath = path; previewNotice = notice; previewKey = previewAsked
    }
    function create() {
        if (!confirmEnabled) return
        if (finalId !== "") { finish(finalId, null); return }
        failure = ""
        if (makeFolder && createdId === "") {
            // The approval: exactly the path shown, for exactly these inputs.
            sent = { name: name, text: instruction, classification: classification, create: true,
                     path: previewPath, onDesktop: onDesktop }
            waiting = true
            bridge.createProjectWithFolder(sent.name, sent.path, sent.onDesktop)   // folderCreated() reads the answer
            return
        }
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
        if (!waiting || sent.create) return
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
    // The one combined answer to "Create a folder" (the id is from that create's own result).
    function folderCreated(id, linked, notice) {
        if (!waiting || !sent.create) return
        waiting = false
        if (id === "") { failure = notice; return }                     // no project was created
        if (linked) { finish(id, sent); return }
        if (sent.text.trim() !== "") bridge.saveProjectInstruction(id, sent.text, sent.classification)
        finalId = id
        failure = qsTr("The project was created, but its folder was not: %1").arg(notice)
    }
    // Opens the project; `what` carries an instruction still to save (null = saved).
    function finish(id, what) {
        var b = bridge
        failure = ""                        // a finished create keeps a waiting new chat's choice
        close()
        b.showProject(id)
        if (what && what.text.trim() !== "") b.saveProjectInstruction(id, what.text, what.classification)
    }
    Connections {
        target: bridge
        function onProjectsChanged() { dialog.settle() }
        function onProjectFolderPreviewed(generation, path, notice) { dialog.previewed(generation, path, notice) }
        function onProjectFolderCreated(id, linked, notice) { dialog.folderCreated(id, linked, notice) }
    }

    Text { text: qsTr("Project name"); color: Theme.textLabel; font: Theme.pageMeta }
    TextField {
        id: nameField
        objectName: "newProjectName"
        width: parent ? parent.width : 0; height: 36
        selectByMouse: true; color: Theme.text; font: Theme.body; maximumLength: 120
        readOnly: dialog.locked
        placeholderText: qsTr("e.g. Kitchen renovation"); placeholderTextColor: Theme.textDim
        background: Rectangle { radius: 6; color: Theme.surfaceDeep; border.width: 1
                                border.color: nameField.activeFocus ? Theme.commandEdge : Theme.border }
    }
    Row {
        spacing: 8
        Text { anchors.verticalCenter: parent.verticalCenter; text: qsTr("What do you want to do?"); color: Theme.textLabel; font: Theme.pageMeta }
        Text { anchors.verticalCenter: parent.verticalCenter; text: qsTr("optional"); color: Theme.textMuted; font: Theme.caption }
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
                readOnly: dialog.locked
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
            Segmented { segmentPrefix: "newProjectClass_"; implicitHeight: 28; enabled: !dialog.locked
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
        Text { anchors.verticalCenter: parent.verticalCenter; text: qsTr("optional"); color: Theme.textMuted; font: Theme.caption }
    }
    Row {
        spacing: 8
        ActionButton { objectName: "newProjectFolderBrowse"; label: qsTr("Browse…"); height: 36
                       enabled: !dialog.waiting && dialog.finalId === ""; onClicked: folderPicker.open() }
        // K1: Core proposes the path; the shown path is what the click approves.
        ActionButton { objectName: "newProjectFolderCreate"; label: qsTr("Create a folder"); height: 36
                       enabled: !dialog.waiting && !dialog.locked
                       onClicked: { dialog.folder = ""; dialog.makeFolder = true } }
        Item {
            id: desktopToggle
            objectName: "newProjectOnDesktop"
            readonly property bool checked: dialog.onDesktop
            enabled: dialog.makeFolder && !dialog.waiting && !dialog.locked
            opacity: enabled ? 1 : 0.45
            width: desktopBox.width + 8 + desktopLabel.implicitWidth; height: 36
            activeFocusOnTab: true
            Accessible.role: Accessible.CheckBox; Accessible.name: desktopLabel.text; Accessible.checked: checked
            Accessible.onToggleAction: dialog.onDesktop = !dialog.onDesktop
            Keys.onSpacePressed: dialog.onDesktop = !dialog.onDesktop
            Rectangle { id: desktopBox; anchors.verticalCenter: parent.verticalCenter; width: 16; height: 16; radius: 4
                        color: desktopToggle.checked ? Theme.alpha(Theme.commandEdge, 0.25) : "transparent"
                        border.width: desktopToggle.activeFocus ? 2 : 1
                        border.color: desktopToggle.checked || desktopToggle.activeFocus ? Theme.commandEdge : Theme.borderStrong
                        Icon { anchors.centerIn: parent; visible: desktopToggle.checked; name: "check"; size: 12; tint: Theme.commandEdge } }
            Text { id: desktopLabel; x: desktopBox.width + 8; anchors.verticalCenter: parent.verticalCenter
                   text: qsTr("On the Desktop"); color: Theme.textLabel; font: Theme.caption }
            MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: dialog.onDesktop = !dialog.onDesktop }
        }
    }
    Item {
        width: parent ? parent.width : 0; height: Math.max(28, folderText.implicitHeight)
        Text { id: folderText; objectName: "newProjectFolderPath"; anchors.verticalCenter: parent.verticalCenter
               width: parent.width - (clearFolder.visible ? clearFolder.width + 10 : 0); wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText
               text: dialog.folder !== "" ? dialog.folder
                     : !dialog.makeFolder ? qsTr("No folder. A project can always be created without one.")
                     : dialog.previewReady ? dialog.previewPath
                     : dialog.name === "" ? qsTr("Type a name to see where the folder will be created.")
                     : dialog.previewKey === dialog.inputKey && dialog.previewNotice !== "" ? dialog.previewNotice
                     : qsTr("Finding the folder's place…")
               color: dialog.folder !== "" || dialog.previewReady ? Theme.text : Theme.textMuted; font: Theme.caption }
        ActionButton { id: clearFolder; objectName: "newProjectFolderClear"; compact: true; label: qsTr("Remove")
                       anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                       visible: (dialog.folder !== "" || dialog.makeFolder) && !dialog.waiting && dialog.finalId === ""
                       onClicked: { dialog.folder = ""; dialog.makeFolder = false } }
    }
    Text { objectName: "newProjectFolderNote"; width: parent ? parent.width : 0; wrapMode: Text.Wrap
           visible: dialog.folder !== "" || dialog.previewReady
           text: (dialog.previewReady ? qsTr("JARVIS creates exactly this folder when you click Create project. ") : "")
                 + qsTr("JARVIS does not read this folder, write into it or send it to the model: it is context for you only.")
           color: Theme.textMuted; font: Theme.caption }
    FolderDialog {
        id: folderPicker
        objectName: "newProjectFolderPicker"
        onAccepted: { dialog.makeFolder = false; dialog.folder = bridge.folderPath(selectedFolder) }
    }
    Text { objectName: "newProjectReason"; width: parent ? parent.width : 0; wrapMode: Text.Wrap; visible: text !== ""
           color: dialog.overCap ? Theme.warning : Theme.textMuted; font: Theme.caption
           text: dialog.overCap ? qsTr("The instruction can be at most %1 characters.").arg(bridge.projectInstructionCap)
                 : dialog.hasInstruction && dialog.classification === "" ? qsTr("Choose PUBLIC or INTERNAL for the instruction.") : "" }
    Text { objectName: "newProjectNotice"; width: parent ? parent.width : 0; wrapMode: Text.Wrap; visible: text !== ""
           textFormat: Text.PlainText; text: dialog.failure; color: Theme.warning; font: Theme.caption }

    buttons: [
        ActionButton { objectName: "newProjectCancel"; height: 36; width: dialog.createdId !== "" ? implicitWidth : 110
                       visible: dialog.finalId === ""
                       label: dialog.createdId !== "" ? qsTr("Continue without a folder") : qsTr("Cancel")
                       onClicked: dialog.createdId !== "" ? dialog.finish(dialog.createdId, null) : dialog.close() },
        ActionButton { id: createButton; objectName: "newProjectCreate"; primary: true; height: 36
                       label: dialog.finalId !== "" ? qsTr("OK") : dialog.createdId !== "" ? qsTr("Link folder") : qsTr("Create project")
                       onClicked: dialog.create() }
    ]
}
