import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Dialogs
import "../theme"
import "../components"

// Projects (V2.0): the window's view of the project model. Names are shown;
// ids stay internal. Every change goes through the existing explicit-user
// paths and Core's answer is shown as returned. A project is context, never
// authority: nothing here approves, grants or selects anything.
SecondaryPage {
    id: page
    objectName: "projectsPage"
    title: qsTr("Projects")
    headerRight: [ ActionButton { label: qsTr("Refresh"); onClicked: bridge.refreshProjects() } ]
    readonly property var active: bridge.projectRecords.filter(function(p) { return p.active })
    readonly property var archived: bridge.projectRecords.filter(function(p) { return !p.active })
    readonly property var current: bridge.openProject
    // Gate 6B: the open detail section and the editor's / panel's chosen
    // classifications ("" = not chosen: nothing is preselected).
    property string section: "conversations"
    property string instructionClass: ""
    property string fileClass: ""
    // The editor shows the STORED instruction: loaded when another project or
    // a new stored version arrives, never on an unrelated refresh.
    function loadInstruction() {
        instructionEditor.text = bridge.projectInstruction.text || ""
        instructionClass = bridge.projectInstruction.classification || ""
    }
    Connections { target: bridge; function onProjectInstructionLoaded() { page.loadInstruction() } }
    Component.onCompleted: loadInstruction()
    function number(n) { return Number(n || 0).toLocaleString(Qt.locale(i18n.qtLocale), "f", 0) }
    function size(bytes) {
        return bytes >= 1048576 ? qsTr("%1 MB").arg((bytes / 1048576).toLocaleString(Qt.locale(i18n.qtLocale), "f", 1))
             : bytes >= 1024 ? qsTr("%1 KB").arg((bytes / 1024).toLocaleString(Qt.locale(i18n.qtLocale), "f", 1))
             : qsTr("%1 bytes").arg(bytes || 0)
    }
    function fileMeta(file) {
        if (!file || !file.id) return ""
        return [file.lifecycle === "active" ? qsTr("active") : file.lifecycle === "retired" ? qsTr("retired") : String(file.lifecycle),
                String(file.classification || "").toUpperCase(), size(file.byte_size),
                qsTr("added %1").arg(when(file.added_at))].join("  ·  ")
    }

    component Field: TextField {
        selectByMouse: true; color: Theme.text; font: Theme.body; maximumLength: 120
        placeholderTextColor: Theme.textDim
        background: Rectangle { radius: 6; color: Theme.surfaceDeep; border.width: 1
                                border.color: parent.activeFocus ? Theme.commandEdge : Theme.border }
    }
    component Choice: Rectangle {
        property string value: ""
        property bool selected: false
        signal picked()
        objectName: "classChoice"
        width: choiceText.implicitWidth + 20; height: 28; radius: 14
        opacity: enabled ? 1 : 0.45
        color: selected ? Theme.alpha(Theme.accent, 0.16) : choiceArea.containsMouse ? Theme.alpha(Theme.text, 0.06) : "transparent"
        border.width: 1; border.color: selected ? Theme.alpha(Theme.accent, 0.7) : Theme.border
        Accessible.role: Accessible.RadioButton; Accessible.name: choiceText.text; Accessible.checked: selected
        Text { id: choiceText; anchors.centerIn: parent; text: parent.value.toUpperCase(); color: parent.selected ? Theme.accent : Theme.text; font: Theme.caption }
        MouseArea { id: choiceArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: parent.picked() }
    }
    component ProjectRow: Rectangle {
        property var project: ({})
        objectName: "projectRow"
        width: parent ? parent.width : 0; height: 44; radius: Theme.radiusSmall
        readonly property bool current: page.current.id === project.id
        color: current ? Theme.alpha(Theme.accent, 0.10) : "transparent"
        border.width: 1; border.color: current ? Theme.alpha(Theme.accent, 0.6) : Theme.border
        Text { objectName: "projectRowName"; anchors { left: parent.left; leftMargin: 12; right: unarchive.left; rightMargin: 8; verticalCenter: parent.verticalCenter }
               text: parent.project.name || ""; textFormat: Text.PlainText; elide: Text.ElideRight
               color: parent.project.active ? Theme.text : Theme.textMuted; font: Theme.pageRowStrong }
        MouseArea { anchors.fill: parent; onClicked: bridge.showProject(parent.project.id) }
        ActionButton { id: unarchive; objectName: "projectUnarchive"; compact: true; visible: !parent.project.active
                       anchors { right: parent.right; rightMargin: 6; verticalCenter: parent.verticalCenter }
                       width: visible ? implicitWidth : 0; label: qsTr("Unarchive")
                       onClicked: bridge.unarchiveProject(parent.project.id) }
    }

    Row {
        anchors.fill: parent
        spacing: Theme.gap

        // -- the list: active projects, then the always-visible Archived section --
        Column {
            width: parent.width * 0.38 - Theme.gap / 2; height: parent.height; spacing: 8
            Row {
                width: parent.width; spacing: 8
                Field { id: newName; objectName: "projectNewName"; width: parent.width - createButton.width - 8; height: 36
                        placeholderText: qsTr("New project name"); onAccepted: createButton.clicked() }
                ActionButton { id: createButton; objectName: "projectCreate"; label: qsTr("Create"); primary: true; height: 36; width: 100
                               onClicked: { bridge.createProject(newName.text); if (bridge.projectNotice === "") newName.text = "" } }
            }
            Text { objectName: "projectNotice"; visible: bridge.projectNotice !== ""; width: parent.width; wrapMode: Text.Wrap
                   textFormat: Text.PlainText; text: bridge.projectNotice; color: Theme.warning; font: Theme.pageRow }
            Flickable {
                width: parent.width; height: parent.height - y; clip: true
                contentHeight: lists.height; boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar {}
                Column {
                    id: lists; width: parent.width; spacing: 6
                    Text { text: qsTr("ACTIVE"); color: Theme.textLabel; font: Theme.pageSection }
                    Text { visible: page.active.length === 0; text: qsTr("No active project yet."); color: Theme.textMuted; font: Theme.pageMeta }
                    Repeater { model: page.active; ProjectRow { project: modelData } }
                    Item { width: 1; height: 8; visible: page.archived.length > 0 }
                    Text { objectName: "projectsArchivedHeading"; visible: page.archived.length > 0; text: qsTr("ARCHIVED"); color: Theme.textLabel; font: Theme.pageSection }
                    Repeater { model: page.archived; ProjectRow { project: modelData } }
                }
            }
        }

        // -- the detail of the open project ---------------------------------------
        Item {
            width: parent.width * 0.62 - Theme.gap / 2; height: parent.height
            Text { visible: !page.current.id; anchors.centerIn: parent; text: qsTr("Choose a project to see its conversations.")
                   color: Theme.textMuted; font: Theme.pageRow }
            Column {
                id: detail
                objectName: "projectDetail"
                visible: !!page.current.id
                anchors.fill: parent; spacing: 10
                Text { objectName: "projectDetailName"; width: parent.width; text: page.current.name || ""; textFormat: Text.PlainText
                       elide: Text.ElideRight; color: Theme.text; font: Theme.title }
                Rectangle {
                    objectName: "projectArchivedBanner"
                    visible: page.current.active === false
                    width: parent.width; height: visible ? archivedText.height + 20 : 0; radius: Theme.radiusSmall
                    color: Theme.alpha(Theme.warning, 0.08); border.width: 1; border.color: Theme.alpha(Theme.warning, 0.5)
                    Text { id: archivedText; x: 12; y: 10; width: parent.width - 24; wrapMode: Text.Wrap; color: Theme.warning; font: Theme.pageRow
                           text: qsTr("Archived: this project is read-only. Its conversations are kept and can be read, but nothing new runs in it until you unarchive it.") }
                }
                Row {
                    spacing: 8
                    Field { id: renameField; objectName: "projectRenameField"; width: 260; height: 36; enabled: page.current.active === true
                            placeholderText: qsTr("New name") }
                    ActionButton { objectName: "projectRename"; label: qsTr("Rename"); height: 36; enabled: page.current.active === true
                                   onClicked: { bridge.renameProject(page.current.id, renameField.text); if (bridge.projectNotice === "") renameField.text = "" } }
                    ActionButton { objectName: "projectArchive"; visible: page.current.active === true; label: qsTr("Archive"); danger: true; height: 36
                                   onClicked: bridge.archiveProject(page.current.id) }
                    ActionButton { objectName: "projectDetailUnarchive"; visible: page.current.active === false; label: qsTr("Unarchive"); height: 36
                                   onClicked: bridge.unarchiveProject(page.current.id) }
                }
                // Gate 6B: the project's conversations, its instruction and its files.
                Row {
                    spacing: 6
                    Repeater {
                        model: [{ id: "conversations", label: qsTr("Conversations") },
                                { id: "instruction", label: qsTr("Instruction") },
                                { id: "files", label: qsTr("Files") }]
                        delegate: ActionButton { objectName: "projectTab_" + modelData.id; compact: true; label: modelData.label
                                                 primary: page.section === modelData.id; onClicked: page.section = modelData.id }
                    }
                }
                Item {
                    width: parent.width; height: parent.height - y

                    // -- conversations and jobs (Gate 6A) --
                    Column {
                        visible: page.section === "conversations"
                        anchors.fill: parent; spacing: 10
                        Item { width: parent.width; height: 36
                            Text { anchors.verticalCenter: parent.verticalCenter; text: qsTr("CONVERSATIONS"); color: Theme.textLabel; font: Theme.pageSection }
                            ActionButton { objectName: "projectNewConversation"; anchors.right: parent.right; compact: true
                                           enabled: page.current.active === true
                                           label: qsTr("New conversation in %1").arg(page.current.name || "")
                                           onClicked: { var b = bridge; if (b.newProjectConversation(page.current.id)) b.setPage("home") } } }
                        ListView {
                            id: conversationList
                            objectName: "projectConversationList"
                            width: parent.width; height: Math.min(Math.max(contentHeight, 40), parent.height * 0.5); clip: true; spacing: 4
                            model: bridge.projectConversations
                            ScrollBar.vertical: ScrollBar {}
                            delegate: Rectangle {
                                objectName: "projectConversationRow"
                                width: ListView.view.width; height: 40; radius: Theme.radiusSmall
                                color: modelData.active ? Theme.alpha(Theme.accent, 0.10) : "transparent"
                                border.width: 1; border.color: modelData.active ? Theme.alpha(Theme.accent, 0.6) : Theme.border
                                Text { anchors { left: parent.left; leftMargin: 10; right: parent.right; rightMargin: 10; verticalCenter: parent.verticalCenter }
                                       text: modelData.title + "  ·  " + (modelData.active ? qsTr("open") : page.when(modelData.updated_at))
                                       textFormat: Text.PlainText; color: modelData.active ? Theme.accent : Theme.text; font: Theme.pageRowStrong; elide: Text.ElideRight }
                                MouseArea { anchors.fill: parent; onClicked: { var b = bridge, id = modelData.id; b.openConversation(id); b.setPage("home") } }
                            }
                            Text { visible: conversationList.count === 0; anchors.centerIn: parent; text: qsTr("No conversation in this project yet.")
                                   color: Theme.textMuted; font: Theme.pageMeta }
                        }
                        Text { objectName: "projectJobsHeading"; width: parent.width; wrapMode: Text.Wrap; color: Theme.textLabel; font: Theme.pageSection
                               text: bridge.projectJobsBounded
                                     ? qsTr("SCHEDULED JOBS · among the first %1 jobs created; this project may have more").arg(bridge.projectJobsLimit)
                                     : qsTr("SCHEDULED JOBS") }
                        ListView {
                            id: jobList
                            objectName: "projectJobList"
                            width: parent.width; height: parent.height - y; clip: true; spacing: 4
                            model: bridge.projectJobs
                            ScrollBar.vertical: ScrollBar {}
                            delegate: Rectangle {
                                width: ListView.view.width; height: 36; radius: Theme.radiusSmall; color: "transparent"; border.width: 1; border.color: Theme.border
                                Text { anchors { left: parent.left; leftMargin: 10; right: parent.right; rightMargin: 10; verticalCenter: parent.verticalCenter }
                                       textFormat: Text.PlainText; elide: Text.ElideRight; color: Theme.text; font: Theme.pageRow
                                       text: (modelData.readable ? modelData.label : qsTr("(unreadable definition)")) + "  ·  "
                                             + (modelData.archived ? qsTr("archived") : modelData.enabled ? qsTr("enabled") : qsTr("disabled")) }
                            }
                            Text { visible: jobList.count === 0; anchors.centerIn: parent; text: qsTr("No scheduled job for this project."); color: Theme.textMuted; font: Theme.pageMeta }
                        }
                    }

                    // -- the instruction editor --
                    // Guidance for the model, never authority. The store decides
                    // (2,000-character cap, PUBLIC/INTERNAL, archive refusal) and
                    // redacts; what it stored is what this editor shows after a save.
                    Column {
                        objectName: "instructionPanel"
                        visible: page.section === "instruction"
                        anchors.fill: parent; spacing: 8
                        Text { objectName: "instructionPrecedence"; width: parent.width; wrapMode: Text.Wrap; color: Theme.textMuted; font: Theme.pageMeta
                               text: qsTr("Order: JARVIS safety rules and Core decisions, then your current message, then this instruction, then retrieved memory. The instruction guides the model; it never grants a permission or approves anything.") }
                        Rectangle {
                            width: parent.width; height: Math.max(120, parent.height - y - instructionFooter.height - 8); radius: 6
                            color: Theme.surfaceDeep; border.width: 1; border.color: instructionEditor.activeFocus ? Theme.commandEdge : Theme.border
                            Flickable {
                                id: instructionFlick
                                anchors.fill: parent; anchors.margins: 10; clip: true
                                contentHeight: instructionEditor.contentHeight; boundsBehavior: Flickable.StopAtBounds
                                ScrollBar.vertical: ScrollBar {}
                                TextEdit {
                                    id: instructionEditor
                                    objectName: "instructionEditor"
                                    width: instructionFlick.width
                                    textFormat: TextEdit.PlainText; wrapMode: TextEdit.Wrap; selectByMouse: true
                                    readOnly: page.current.active !== true
                                    color: Theme.text; font: Theme.body
                                }
                            }
                        }
                        Column {
                            id: instructionFooter
                            width: parent.width; spacing: 6
                            Row {
                                spacing: 8
                                Text { objectName: "instructionCounter"; anchors.verticalCenter: parent.verticalCenter; width: 110
                                       text: qsTr("%1 / %2").arg(instructionEditor.length).arg(bridge.projectInstructionCap)
                                       color: instructionEditor.length > bridge.projectInstructionCap ? Theme.warning : Theme.textMuted; font: Theme.pageMetaFigures }
                                Text { anchors.verticalCenter: parent.verticalCenter; text: qsTr("Classification:"); color: Theme.textLabel; font: Theme.pageMeta }
                                Repeater {
                                    model: ["public", "internal"]
                                    delegate: Choice { objectName: "instructionClass_" + modelData; value: modelData; enabled: page.current.active === true
                                                       selected: page.instructionClass === modelData; onPicked: page.instructionClass = modelData }
                                }
                                ActionButton { objectName: "instructionClear"; compact: true; label: qsTr("Clear")
                                               enabled: page.current.active === true && page.instructionClass !== ""
                                               onClicked: bridge.saveProjectInstruction(page.current.id, "", page.instructionClass) }
                                ActionButton { objectName: "instructionSave"; compact: true; primary: true; label: qsTr("Save")
                                               enabled: page.current.active === true && page.instructionClass !== ""
                                               onClicked: bridge.saveProjectInstruction(page.current.id, instructionEditor.text, page.instructionClass) }
                            }
                            Text { objectName: "instructionReason"; width: parent.width; wrapMode: Text.Wrap; font: Theme.pageMeta
                                   visible: text !== ""; color: page.current.active === true ? Theme.textMuted : Theme.warning
                                   text: page.current.active !== true ? qsTr("Archived project: the instruction is read-only.")
                                         : page.instructionClass === "" ? qsTr("Choose PUBLIC or INTERNAL before saving.") : "" }
                            Text { objectName: "instructionNotice"; width: parent.width; wrapMode: Text.Wrap; visible: text !== ""
                                   textFormat: Text.PlainText; text: bridge.projectInstructionNotice; color: Theme.warning; font: Theme.pageMeta }
                        }
                    }

                    // -- the file panel --
                    // Files are untrusted data, never instructions. Counters come
                    // from the store's listing; its refusals are shown as returned.
                    Column {
                        objectName: "filePanel"
                        visible: page.section === "files"
                        anchors.fill: parent; spacing: 8
                        readonly property var counts: bridge.projectFileCounts
                        Row {
                            spacing: 18
                            Text { objectName: "fileCountActive"; color: Theme.textLabel; font: Theme.pageMetaFigures
                                   text: qsTr("Active %1/%2").arg(page.number(parent.parent.counts.active)).arg(page.number(parent.parent.counts.active_max)) }
                            Text { objectName: "fileCountRetained"; color: Theme.textLabel; font: Theme.pageMetaFigures
                                   text: qsTr("Retained %1/%2").arg(page.number(parent.parent.counts.retained)).arg(page.number(parent.parent.counts.retained_max)) }
                            Text { objectName: "fileCountChars"; color: Theme.textLabel; font: Theme.pageMetaFigures
                                   text: qsTr("Characters %1/%2").arg(page.number(parent.parent.counts.chars)).arg(page.number(parent.parent.counts.chars_max)) }
                        }
                        Row {
                            spacing: 8
                            Text { anchors.verticalCenter: parent.verticalCenter; text: qsTr("Classification:"); color: Theme.textLabel; font: Theme.pageMeta }
                            Repeater {
                                model: ["public", "internal", "sensitive", "secret"]
                                delegate: Choice { objectName: "fileClass_" + modelData; value: modelData; enabled: page.current.active === true
                                                   selected: page.fileClass === modelData; onPicked: page.fileClass = modelData }
                            }
                            ActionButton { objectName: "fileAdd"; compact: true; primary: true; label: qsTr("Add file…")
                                           enabled: page.current.active === true && page.fileClass !== ""
                                           onClicked: filePicker.open() }
                        }
                        Text { objectName: "fileReason"; width: parent.width; wrapMode: Text.Wrap; font: Theme.pageMeta
                               visible: text !== ""; color: page.current.active === true ? Theme.textMuted : Theme.warning
                               text: page.current.active !== true ? qsTr("Archived project: files cannot be added, retired or restored. Purge stays available.")
                                     : page.fileClass === "" ? qsTr("Choose a classification before adding a file. SECRET files are stored but never retrieved.") : "" }
                        Text { objectName: "fileNotice"; width: parent.width; wrapMode: Text.Wrap; visible: text !== ""
                               textFormat: Text.PlainText; text: bridge.projectFileNotice; color: Theme.warning; font: Theme.pageMeta }
                        ListView {
                            id: fileList
                            objectName: "fileList"
                            width: parent.width; height: parent.height - y; clip: true; spacing: 4
                            model: bridge.projectFiles
                            ScrollBar.vertical: ScrollBar {}
                            delegate: Rectangle {
                                objectName: "fileRow"
                                property var file: modelData
                                width: ListView.view.width; height: 52; radius: Theme.radiusSmall; color: "transparent"
                                border.width: 1; border.color: Theme.border
                                Column {
                                    anchors { left: parent.left; leftMargin: 10; right: fileButtons.left; rightMargin: 10; verticalCenter: parent.verticalCenter }
                                    spacing: 2
                                    Text { objectName: "fileRowName"; width: parent.width; text: file.display_name || ""; textFormat: Text.PlainText
                                           elide: Text.ElideRight; color: file.lifecycle === "active" ? Theme.text : Theme.textMuted; font: Theme.pageRowStrong }
                                    Text { objectName: "fileRowMeta"; width: parent.width; textFormat: Text.PlainText; elide: Text.ElideRight
                                           color: Theme.textMuted; font: Theme.pageMeta; text: page.fileMeta(file) }
                                }
                                Row {
                                    id: fileButtons
                                    anchors { right: parent.right; rightMargin: 8; verticalCenter: parent.verticalCenter }
                                    spacing: 6
                                    ActionButton { objectName: "fileRetire"; compact: true; label: qsTr("Retire"); visible: file.lifecycle === "active"
                                                   enabled: page.current.active === true; onClicked: bridge.retireProjectFile(page.current.id, file.id) }
                                    ActionButton { objectName: "fileRestore"; compact: true; label: qsTr("Restore"); visible: file.lifecycle === "retired"
                                                   enabled: page.current.active === true; onClicked: bridge.restoreProjectFile(page.current.id, file.id) }
                                    ActionButton { objectName: "filePurge"; compact: true; danger: true; label: qsTr("Purge…"); visible: file.purgeable === true
                                                   onClicked: purgeDialog.openFor(file) }
                                }
                            }
                            Text { visible: fileList.count === 0; anchors.centerIn: parent; text: qsTr("No file in this project yet.")
                                   color: Theme.textMuted; font: Theme.pageMeta }
                        }
                    }
                }
            }
        }
    }

    FileDialog {
        id: filePicker
        objectName: "projectFileDialog"
        fileMode: FileDialog.OpenFile
        nameFilters: [qsTr("Text and code (%1)").arg("*.txt *.md *.json *.yaml *.yml *.csv *.log *.py *.js *.ts *.qml *.html *.css *.xml *.sh *.sql")]
        onAccepted: bridge.addProjectFile(page.current.id, selectedFile, page.fileClass)
    }

    // Purge: the permanent second step. The dialog names the exact row (names
    // are not unique), says honestly what purge does not reach (ARCHITECTURE
    // 4.24) and acts only when that row's file name is typed exactly.
    Popup {
        id: purgeDialog
        objectName: "purgeDialog"
        property var file: ({})
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: 560; padding: 18
        modal: true; focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        function openFor(row) { file = row; confirmName.text = ""; open(); confirmName.forceActiveFocus() }
        background: Rectangle { radius: 12; color: Theme.glassOverlay; border.width: 1; border.color: Theme.glassOverlayEdge }
        contentItem: Column {
            spacing: 12
            Text { text: qsTr("Purge this file permanently?"); color: Theme.text; font: Theme.uiValue }
            Text { objectName: "purgeIdentity"; width: parent.width; wrapMode: Text.Wrap; textFormat: Text.PlainText
                   color: Theme.text; font: Theme.pageRowStrong; text: purgeDialog.file.display_name || "" }
            Text { objectName: "purgeIdentityMeta"; width: parent.width; wrapMode: Text.Wrap; textFormat: Text.PlainText
                   color: Theme.textMuted; font: Theme.pageMeta; text: page.fileMeta(purgeDialog.file) }
            Text { objectName: "purgeResidue"; width: parent.width; wrapMode: Text.Wrap; color: Theme.textLabel; font: Theme.pageMeta
                   text: qsTr("Purge deletes this file's text from the project store for good and frees its quota; a tombstone (id, fingerprint, time) remains. It does NOT reach: copies already sent to a provider, approval previews and audit records that showed excerpts, task results and conversation history, database backups (such as jarvis.pre-v2.db), or leftover data in database free pages, the WAL, the file system or the disk.") }
            Text { width: parent.width; wrapMode: Text.Wrap; textFormat: Text.PlainText; color: Theme.text; font: Theme.pageMeta
                   text: qsTr("Type the file name to confirm: %1").arg(purgeDialog.file.display_name || "") }
            Field { id: confirmName; objectName: "purgeConfirmName"; width: parent.width; height: 36; maximumLength: 1024 }
            Row {
                anchors.right: parent.right
                spacing: 8
                ActionButton { objectName: "purgeCancel"; label: qsTr("Cancel"); height: 36; width: 110; onClicked: purgeDialog.close() }
                ActionButton { objectName: "purgeConfirm"; label: qsTr("Purge"); danger: true; primary: true; height: 36; width: 110
                               enabled: confirmName.text === (purgeDialog.file.display_name || null)
                               onClicked: { if (bridge.purgeProjectFile(page.current.id, purgeDialog.file.id, confirmName.text)) purgeDialog.close() } }
            }
        }
    }
}
