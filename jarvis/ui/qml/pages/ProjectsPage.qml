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
        id: chip
        property string value: ""
        property bool selected: false
        signal picked()
        objectName: "classChoice"
        width: choiceText.implicitWidth + 20; height: 28; radius: 14
        opacity: enabled ? 1 : 0.45
        color: selected ? Theme.alpha(Theme.accent, 0.16) : choiceArea.containsMouse ? Theme.alpha(Theme.text, 0.06) : "transparent"
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
        border.width: 1; border.color: selected ? Theme.alpha(Theme.accent, 0.7) : Theme.border
        activeFocusOnTab: enabled
        Accessible.role: Accessible.RadioButton; Accessible.name: choiceText.text; Accessible.checked: selected
        Keys.onReturnPressed: chip.picked()
        Keys.onSpacePressed: chip.picked()
        Text { id: choiceText; anchors.centerIn: parent; text: parent.value.toUpperCase(); color: parent.selected ? Theme.accent : Theme.text; font: Theme.caption }
        MouseArea { id: choiceArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: parent.picked() }
        Rectangle { objectName: "classChoiceFocusRing"; visible: chip.activeFocus; anchors.fill: parent; anchors.margins: -4; radius: parent.radius + 4
                    color: "transparent"; border.width: 2; border.color: Theme.alpha(Theme.materialGlint, 0.95) }
    }
    // A disabled control takes no hover, so its reason tip sits beside it, in
    // this wrapper (the control is the wrapper's last child).
    component Reasoned: Item {
        id: reasoned
        property string reason: ""
        readonly property Item control: children[children.length - 1]
        implicitWidth: control ? control.width : 0; implicitHeight: control ? control.height : 0
        HoverTip { target: reasoned.control; text: reasoned.reason; active: !!reasoned.control && !reasoned.control.enabled }
    }
    component ProjectRow: ListRow {
        id: projectRow
        property var project: ({})
        objectName: "projectRow"
        titleName: "projectRowName"
        title: project.name || ""; current: page.current.id === project.id; muted: !project.active
        badge: project.active ? "" : qsTr("ARCHIVED"); badgeTint: Theme.warning
        onActivated: bridge.showProject(projectRow.project.id)
        ActionButton { objectName: "projectUnarchive"; compact: true; visible: !projectRow.project.active; label: qsTr("Unarchive")
                       onClicked: bridge.unarchiveProject(projectRow.project.id) }
    }
    // Gate 6E: the open project's detail fades in when another project opens;
    // Q6: the rename field shows only after "Rename" (Enter saves, Esc or
    // Cancel puts it away; the same bridge call as before).
    property bool renaming: false
    readonly property string shownId: current.id || ""
    onShownIdChanged: { renaming = false; if (Theme.ambient) detailFade.restart() }
    function startRename() {
        renaming = true
        renameField.text = current.name || ""
        renameField.selectAll(); renameField.forceActiveFocus()
    }
    function stopRename() { renaming = false; renameButton.forceActiveFocus() }
    function saveRename() {
        bridge.renameProject(current.id, renameField.text)
        if (bridge.projectNotice === "") stopRename()
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
                id: listColumn
                width: parent.width; height: parent.height - y; clip: true
                contentHeight: lists.height; boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar { policy: listColumn.contentHeight > listColumn.height ? ScrollBar.AlwaysOn : ScrollBar.AsNeeded }
                Column {
                    id: lists; width: parent.width - (listColumn.contentHeight > listColumn.height ? 12 : 0); spacing: 6
                    Text { text: qsTr("ACTIVE"); color: Theme.textLabel; font: Theme.pageSection }
                    Text { visible: page.active.length === 0; text: qsTr("No active project yet."); color: Theme.textLabel; font: Theme.pageMeta }
                    Repeater { model: page.active; ProjectRow { project: modelData } }
                    Item { width: 1; height: 8; visible: page.archived.length > 0 }
                    Text { objectName: "projectsArchivedHeading"; visible: page.archived.length > 0; text: qsTr("ARCHIVED PROJECTS"); color: Theme.textLabel; font: Theme.pageSection }
                    Repeater { model: page.archived; ProjectRow { project: modelData } }
                }
            }
        }

        // -- the detail of the open project ---------------------------------------
        Item {
            width: parent.width * 0.62 - Theme.gap / 2; height: parent.height
            // Gate 6E (D23): compact lines at the top, never floating mid-void.
            Text { objectName: "projectsEmpty"; visible: !page.current.id; width: parent.width; wrapMode: Text.Wrap
                   text: bridge.projectRecords.length === 0 ? qsTr("No project yet. Type a name in the field on the left and press Create.")
                                                           : qsTr("Choose a project to see its conversations.")
                   color: Theme.textLabel; font: Theme.pageRow }
            Column {
                id: detail
                objectName: "projectDetail"
                visible: !!page.current.id
                anchors.fill: parent; spacing: 10
                NumberAnimation { id: detailFade; target: detail; property: "opacity"; from: 0; to: 1; duration: Theme.panelIn; easing.type: Theme.ease }
                // The name (elided; its tip has it whole), its state pill, the actions.
                Item {
                    width: parent.width; height: 36
                    Text { id: detailName; objectName: "projectDetailName"; anchors.verticalCenter: parent.verticalCenter
                           width: Math.max(0, Math.min(implicitWidth, parent.width - statePill.width - 12 - detailActions.width - 16))
                           text: page.current.name || ""; textFormat: Text.PlainText
                           elide: Text.ElideRight; color: Theme.text; font: Theme.titleLarge }
                    HoverTip { objectName: "projectDetailNameTip"; target: detailName; text: detailName.text; active: detailName.truncated }
                    Pill { id: statePill; objectName: "projectStatePill"; x: detailName.width + 12; anchors.verticalCenter: parent.verticalCenter
                           font: Theme.pageSection; implicitHeight: 24
                           text: page.current.active === false ? qsTr("archived") : qsTr("active")
                           tint: page.current.active === false ? Theme.warning : Theme.success }
                    Row {
                        id: detailActions
                        anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; spacing: 6
                        ActionButton { id: renameButton; objectName: "projectRename"; compact: true; label: qsTr("Rename")
                                       visible: page.current.active === true && !page.renaming; onClicked: page.startRename() }
                        ActionButton { objectName: "projectArchive"; compact: true; visible: page.current.active === true; label: qsTr("Archive"); danger: true
                                       onClicked: bridge.archiveProject(page.current.id) }
                        ActionButton { objectName: "projectDetailUnarchive"; compact: true; visible: page.current.active === false; label: qsTr("Unarchive")
                                       onClicked: bridge.unarchiveProject(page.current.id) }
                    }
                }
                Row {
                    objectName: "projectRenameRow"
                    visible: page.renaming && page.current.active === true; spacing: 8
                    Field { id: renameField; objectName: "projectRenameField"; width: Math.min(320, detail.width - 240); height: 36
                            placeholderText: qsTr("New name"); onAccepted: page.saveRename()
                            // Esc here puts the field away; it must not reach the window's Escape (leave the page).
                            Keys.onShortcutOverride: function (event) { event.accepted = event.key === Qt.Key_Escape }
                            Keys.onEscapePressed: page.stopRename() }
                    ActionButton { objectName: "projectRenameSave"; label: qsTr("Save"); primary: true; height: 36; onClicked: page.saveRename() }
                    ActionButton { objectName: "projectRenameCancel"; label: qsTr("Cancel"); height: 36; onClicked: page.stopRename() }
                }
                Rectangle {
                    objectName: "projectArchivedBanner"
                    visible: page.current.active === false
                    width: parent.width; height: visible ? archivedText.height + 20 : 0; radius: Theme.radiusSmall
                    color: Theme.alpha(Theme.warning, 0.08); border.width: 1; border.color: Theme.alpha(Theme.warning, 0.5)
                    Text { id: archivedText; x: 12; y: 10; width: parent.width - 24; wrapMode: Text.Wrap; color: Theme.warning; font: Theme.pageRow
                           text: qsTr("Archived: this project is read-only. Its conversations are kept and can be read, but nothing new runs in it until you unarchive it.") }
                }
                // Gate 6B: the project's conversations, its instruction and its files
                // (Gate 6E: a segmented well, not three filled action buttons).
                Segmented {
                    objectName: "projectTabs"
                    segmentPrefix: "projectTab_"
                    options: [{ value: "conversations", label: qsTr("Conversations") },
                              { value: "instruction", label: qsTr("Instruction") },
                              { value: "files", label: qsTr("Files") }]
                    value: page.section
                    onPicked: function (value) { page.section = value }
                }
                Item {
                    width: parent.width; height: parent.height - y

                    // -- conversations and jobs (Gate 6A); Gate 6E: one scroll (G7) --
                    Flickable {
                        id: conversationsColumn
                        objectName: "projectConversationsColumn"
                        visible: page.section === "conversations"
                        anchors.fill: parent; clip: true
                        contentHeight: conversations.height; boundsBehavior: Flickable.StopAtBounds
                        ScrollBar.vertical: ScrollBar { policy: conversationsColumn.contentHeight > conversationsColumn.height ? ScrollBar.AlwaysOn : ScrollBar.AsNeeded }
                        Column {
                            id: conversations
                            width: conversationsColumn.width - (conversationsColumn.contentHeight > conversationsColumn.height ? 12 : 0); spacing: 10
                            // The heading and its action share the line and never meet (D19):
                            // the action is short; the project's name is in its tip.
                            Item { width: parent.width; height: 36
                                Text { objectName: "projectConversationsHeading"; anchors.verticalCenter: parent.verticalCenter
                                       width: Math.min(implicitWidth, parent.width - newConversation.width - 12); elide: Text.ElideRight
                                       text: qsTr("CONVERSATIONS"); color: Theme.textLabel; font: Theme.pageSection }
                                ActionButton { id: newConversation; objectName: "projectNewConversation"; anchors.right: parent.right; compact: true
                                               enabled: page.current.active === true
                                               label: qsTr("+ New conversation")
                                               onClicked: { var b = bridge; if (b.newProjectConversation(page.current.id)) b.setPage("home") } }
                                HoverTip { objectName: "projectNewConversationTip"; target: newConversation
                                           text: page.current.active === true ? qsTr("New conversation in %1").arg(page.current.name || "")
                                                                             : qsTr("Unarchive the project first") } }
                            Column {
                                width: parent.width; spacing: 4
                                Repeater {
                                    model: bridge.projectConversations
                                    ListRow {
                                        id: conversationRow
                                        objectName: "projectConversationRow"
                                        property var row: modelData
                                        title: row.title; current: row.active
                                        badge: row.active ? qsTr("open") : ""
                                        meta: page.when(row.updated_at)
                                        // Locals: opening rebuilds the model, and this delegate's context with it.
                                        onActivated: { var b = bridge, id = conversationRow.row.id; b.openConversation(id); b.setPage("home") }
                                        // Gate 6D-b: under an archived project the project's archive is
                                        // the reversible first step, so the delete is offered at once.
                                        ActionButton { objectName: "projectConversationDelete"; compact: true; danger: true
                                                       visible: conversationRow.row.deletable === true
                                                       label: qsTr("Delete permanently…"); enabled: !bridge.busy
                                                       onClicked: deleteDialog.openFor(conversationRow.row.id) }
                                    }
                                }
                                Text { objectName: "projectConversationsEmpty"; visible: bridge.projectConversations.length === 0
                                       text: qsTr("No conversation in this project yet."); color: Theme.textLabel; font: Theme.pageMeta }
                            }
                            // Gate 6D-b: the project's archived conversations. A row opens
                            // read-only; unarchiving waits for the project's own unarchive.
                            Text { objectName: "projectArchivedConversationsHeading"; visible: archivedConversationList.count > 0
                                   text: qsTr("ARCHIVED CONVERSATIONS"); color: Theme.textLabel; font: Theme.pageSection }
                            Text { objectName: "projectConversationUnarchiveReason"; visible: archivedConversationList.count > 0 && page.current.active === false
                                   width: parent.width; wrapMode: Text.Wrap; color: Theme.textLabel; font: Theme.pageMeta
                                   text: qsTr("Unarchive the project first") }
                            Text { objectName: "projectConversationNotice"; visible: text !== "" && !deleteDialog.visible; width: parent.width
                                   wrapMode: Text.Wrap; textFormat: Text.PlainText; text: bridge.conversationNotice; color: Theme.warning; font: Theme.pageMeta }
                            ListView {
                                id: archivedConversationList
                                objectName: "projectArchivedConversationList"
                                visible: count > 0
                                // Whole rows, at most five before its own scroll (as History).
                                width: parent.width; height: count <= 5 ? contentHeight : 5 * (contentHeight + spacing) / count - spacing
                                clip: true; spacing: 4; interactive: count > 5; snapMode: ListView.SnapToItem; boundsBehavior: Flickable.StopAtBounds
                                model: bridge.projectArchivedConversations
                                ScrollBar.vertical: ScrollBar { policy: archivedConversationList.count > 5 ? ScrollBar.AlwaysOn : ScrollBar.AsNeeded }
                                delegate: ListRow {
                                    id: archivedRow
                                    objectName: "projectArchivedConversationRow"
                                    property var row: modelData
                                    width: ListView.view.width - (ListView.view.count > 5 ? 12 : 0)
                                    title: row.title; current: row.active; muted: true
                                    badge: qsTr("ARCHIVED"); badgeTint: Theme.warning
                                    meta: qsTr("archived %1").arg(page.when(row.archived_at))
                                    onActivated: { var b = bridge, id = archivedRow.row.id; b.openConversation(id); b.setPage("home") }
                                    Reasoned {
                                        reason: page.current.active === true ? "" : qsTr("Unarchive the project first")
                                        ActionButton { objectName: "projectConversationUnarchive"; compact: true; label: qsTr("Unarchive")
                                                       enabled: page.current.active === true && !bridge.busy
                                                       onClicked: { var b = bridge, id = archivedRow.row.id; b.unarchiveConversation(id) } }
                                    }
                                    ActionButton { objectName: "projectArchivedConversationDelete"; compact: true; danger: true
                                                   label: qsTr("Delete permanently…"); enabled: !bridge.busy; onClicked: deleteDialog.openFor(archivedRow.row.id) }
                                }
                            }
                            Text { objectName: "projectJobsHeading"; width: parent.width; wrapMode: Text.Wrap; color: Theme.textLabel; font: Theme.pageSection
                                   text: bridge.projectJobsBounded
                                         ? qsTr("SCHEDULED JOBS · among the first %1 jobs created; this project may have more").arg(bridge.projectJobsLimit)
                                         : qsTr("SCHEDULED JOBS") }
                            Column {
                                objectName: "projectJobList"
                                width: parent.width; spacing: 4
                                Repeater {
                                    model: bridge.projectJobs
                                    Rectangle {
                                        width: parent.width; height: 36; radius: Theme.radiusSmall; color: "transparent"; border.width: 1; border.color: Theme.border
                                        Text { anchors { left: parent.left; leftMargin: 10; right: parent.right; rightMargin: 10; verticalCenter: parent.verticalCenter }
                                               textFormat: Text.PlainText; elide: Text.ElideRight; color: Theme.text; font: Theme.pageRow
                                               text: (modelData.readable ? modelData.label : qsTr("(unreadable definition)")) + "  ·  "
                                                     + (modelData.archived ? qsTr("archived") : modelData.enabled ? qsTr("enabled") : qsTr("disabled")) }
                                    }
                                }
                                Text { objectName: "projectJobsEmpty"; visible: bridge.projectJobs.length === 0; text: qsTr("No scheduled job for this project.")
                                       color: Theme.textLabel; font: Theme.pageMeta }
                            }
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
                                Reasoned {
                                    reason: instructionReason.text
                                    ActionButton { objectName: "instructionClear"; compact: true; label: qsTr("Clear")
                                                   enabled: page.current.active === true && page.instructionClass !== ""
                                                   onClicked: bridge.saveProjectInstruction(page.current.id, "", page.instructionClass) }
                                }
                                Reasoned {
                                    reason: instructionReason.text
                                    ActionButton { objectName: "instructionSave"; compact: true; primary: true; label: qsTr("Save")
                                                   enabled: page.current.active === true && page.instructionClass !== ""
                                                   onClicked: bridge.saveProjectInstruction(page.current.id, instructionEditor.text, page.instructionClass) }
                                }
                            }
                            Text { id: instructionReason; objectName: "instructionReason"; width: parent.width; wrapMode: Text.Wrap; font: Theme.pageMeta
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
                            Reasoned {
                                reason: fileReason.text
                                ActionButton { objectName: "fileAdd"; compact: true; primary: true; label: qsTr("Add file…")
                                               enabled: page.current.active === true && page.fileClass !== ""
                                               onClicked: filePicker.open() }
                            }
                        }
                        Text { id: fileReason; objectName: "fileReason"; width: parent.width; wrapMode: Text.Wrap; font: Theme.pageMeta
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

    ConversationDeleteDialog { id: deleteDialog }

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
    // Gate 6E: on the shared dialog shell (danger tone; focus starts in the
    // name field, where Enter confirms nothing).
    JarvisDialog {
        id: purgeDialog
        objectName: "purgeDialog"
        property var file: ({})
        danger: true
        preferredWidth: 560
        label: qsTr("Purge")
        title: qsTr("Purge this file permanently?")
        initialFocus: confirmName
        confirmItem: purgeConfirm
        confirmEnabled: confirmName.text === (purgeDialog.file.display_name || null)
        enterGuard: [confirmName]
        function openFor(row) { file = row; confirmName.text = ""; open() }
        Rectangle {
            objectName: "purgeIdentityCard"
            width: parent ? parent.width : 0; height: purgeIdentityColumn.height + 20; radius: Theme.radiusSmall
            color: Theme.glassNested; border.width: 1; border.color: Theme.alpha(Theme.text, 0.10)
            Column {
                id: purgeIdentityColumn
                x: 12; y: 10; width: parent.width - 24; spacing: 4
                Text { objectName: "purgeIdentity"; width: parent.width; wrapMode: Text.Wrap; textFormat: Text.PlainText
                       color: Theme.text; font: Theme.pageRowStrong; text: purgeDialog.file.display_name || "" }
                Text { objectName: "purgeIdentityMeta"; width: parent.width; wrapMode: Text.Wrap; textFormat: Text.PlainText
                       color: Theme.textMuted; font: Theme.pageMeta; text: page.fileMeta(purgeDialog.file) }
            }
        }
        Text { objectName: "purgeResidue"; width: parent ? parent.width : 0; wrapMode: Text.Wrap; color: Theme.textLabel; font: Theme.pageMeta
               text: qsTr("Purge deletes this file's text from the project store for good and frees its quota; a tombstone (id, fingerprint, time) remains. It does NOT reach: copies already sent to a provider, approval previews and audit records that showed excerpts, task results and conversation history, database backups (such as jarvis.pre-v2.db), or leftover data in database free pages, the WAL, the file system or the disk.") }
        Text { width: parent ? parent.width : 0; wrapMode: Text.Wrap; textFormat: Text.PlainText; color: Theme.text; font: Theme.pageMeta
               text: qsTr("Type the file name to confirm: %1").arg(purgeDialog.file.display_name || "") }
        Field { id: confirmName; objectName: "purgeConfirmName"; width: parent ? parent.width : 0; height: 36; maximumLength: 1024 }
        buttons: [
            ActionButton { objectName: "purgeCancel"; label: qsTr("Cancel"); height: 36; width: 110; onClicked: purgeDialog.close() },
            ActionButton { id: purgeConfirm; objectName: "purgeConfirm"; label: qsTr("Purge"); danger: true; primary: true; height: 36; width: 110
                           onClicked: { if (bridge.purgeProjectFile(page.current.id, purgeDialog.file.id, confirmName.text)) purgeDialog.close() } }
        ]
    }
}
