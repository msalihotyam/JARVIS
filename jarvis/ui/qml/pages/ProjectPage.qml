import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Dialogs
import "../theme"
import "../components"

// Gate 6G P3: one project's page (replaces the 6B-6E detail view). The name,
// a "new conversation in this project" composer, its pinned and recent
// conversations (and the archived ones, 6D-b); at the right, rows that open
// the existing instruction editor and file panel as dialogs, and the 6H
// folder link (G6H2: link an EXISTING folder, unlink), and (V2.1a M4) the
// project's typed memory: its approved + pending records in a dialog. No
// Scheduled row before its release (V2.5): nothing fake or empty.
// A project is context, never authority: every change goes through the
// existing explicit-user paths and Core's answer is shown as returned.
// Strings that moved here from the Projects page keep its catalog context.
Item {
    id: projectPage
    objectName: "projectDetail"
    property var project: ({})
    property var host: null                   // the Projects page: its one date formatter
    // Which dialog is open: "conversations" (none), "instruction", "files", "folder" or "memory".
    property string section: "conversations"
    readonly property bool live: project.active === true
    readonly property string shownId: project.id || ""
    function when(v) { return host ? host.when(v) : String(v || "") }
    // R1: a running-only refusal - JARVIS works for this conversation (or a sweep runs).
    function runsIn(id) { return bridge.navigationLocked || bridge.runningConversationId === id }

    // Gate 6B: the editor's / panel's chosen classifications ("" = not chosen: nothing is preselected).
    property string instructionClass: ""
    property string fileClass: ""
    // The editor shows the STORED instruction: loaded when another project or
    // a new stored version arrives, never on an unrelated refresh.
    function loadInstruction() {
        instructionEditor.text = bridge.projectInstruction.text || ""
        instructionClass = bridge.projectInstruction.classification || ""
    }
    Connections { target: bridge; function onProjectInstructionLoaded() { projectPage.loadInstruction() } }
    Component.onCompleted: loadInstruction()
    function number(n) { return Number(n || 0).toLocaleString(Qt.locale(i18n.qtLocale), "f", 0) }
    function size(bytes) {
        return bytes >= 1048576 ? qsTranslate("ProjectsPage", "%1 MB").arg((bytes / 1048576).toLocaleString(Qt.locale(i18n.qtLocale), "f", 1))
             : bytes >= 1024 ? qsTranslate("ProjectsPage", "%1 KB").arg((bytes / 1024).toLocaleString(Qt.locale(i18n.qtLocale), "f", 1))
             : qsTranslate("ProjectsPage", "%1 bytes").arg(bytes || 0)
    }
    function fileMeta(file) {
        if (!file || !file.id) return ""
        return [file.lifecycle === "active" ? qsTranslate("ProjectsPage", "active") : file.lifecycle === "retired" ? qsTranslate("ProjectsPage", "retired") : String(file.lifecycle),
                String(file.classification || "").toUpperCase(), size(file.byte_size),
                qsTranslate("ProjectsPage", "added %1").arg(when(file.added_at))].join("  ·  ")
    }

    // -- the open dialog follows `section` (the Projects page's card menu "Edit" sets it) --
    onSectionChanged: syncSection()
    // Leaving the page (another page loads) takes its dialogs with it: none outlives it open.
    Component.onDestruction: { purgeDialog.close(); filesDialog.close(); instructionDialog.close(); folderDialog.close(); memoryDialog.close() }
    function syncSection() {
        var open = shownId !== ""
        if (section === "instruction" && open) instructionDialog.open()
        else if (instructionDialog.visible) instructionDialog.close()
        if (section === "files" && open) { if (!purgeDialog.visible) filesDialog.open() }
        else if (filesDialog.visible) filesDialog.close()
        if (section === "folder" && open) folderDialog.open()
        else if (folderDialog.visible) folderDialog.close()
        if (section === "memory" && open && live) { memoryDialog.refresh(); memoryDialog.open() }
        else if (memoryDialog.visible) memoryDialog.close()
    }
    // Gate 6E: the page fades in when another project opens; Q6: the rename
    // field shows only after "Rename" (Enter saves, Esc or Cancel puts it away).
    property bool renaming: false
    onShownIdChanged: {
        renaming = false
        if (shownId === "") section = "conversations"
        else if (Theme.ambient) detailFade.restart()
        syncSection()
    }
    NumberAnimation { id: detailFade; target: projectPage; property: "opacity"; from: 0; to: 1; duration: Theme.panelIn; easing.type: Theme.ease }
    function startRename() {
        renaming = true
        renameField.text = project.name || ""
        renameField.selectAll(); renameField.forceActiveFocus()
    }
    function stopRename() { renaming = false; renameButton.forceActiveFocus() }
    function saveRename() {
        bridge.renameProject(project.id, renameField.text)
        if (bridge.projectNotice === "") stopRename()
    }
    // "Bu projede yeni sohbet": a new conversation OWNED by this project (the
    // store's own creation path decides), opened; typed text is its first message.
    // The composer's send (G6SB3F): true = accepted, the draft clears and home opens.
    function start(text) {
        var b = bridge
        if (!live || !b.newProjectConversation(project.id)) return false
        return text.trim() === "" || b.submitText(text)                  // refused: the draft stays here
    }

    component Field: TextField {
        id: field
        selectByMouse: true; color: Theme.text; font: Theme.body; maximumLength: 120
        placeholderTextColor: Theme.textDim
        background: Rectangle { radius: 6; color: Theme.surfaceDeep; border.width: 1
                                border.color: parent.activeFocus ? Theme.commandEdge : Theme.border }
        ContextMenu.menu: null
        EditMenu { objectName: field.objectName + "Menu"; editor: field }
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
    // One conversation of this project; under an archived project the
    // project's archive is the reversible first step, so the delete is offered at once (6D-b).
    component ConversationRow: ListRow {
        id: conversationRow
        property var row: ({})
        title: row.title || ""; current: !!row.active
        badge: row.active ? qsTranslate("ProjectsPage", "open") : ""
        meta: projectPage.when(row.updated_at)
        // Locals: opening rebuilds the model, and this delegate's context with it.
        onActivated: { var b = bridge, id = conversationRow.row.id; b.openConversation(id); b.setPage("home") }
        ActionButton { objectName: "projectConversationDelete"; compact: true; danger: true
                       visible: conversationRow.row.deletable === true
                       label: qsTranslate("ProjectsPage", "Delete permanently…"); enabled: !projectPage.runsIn(conversationRow.row.id)
                       onClicked: deleteDialog.openFor(conversationRow.row.id) }
    }
    // A right-column row: an icon, its name, what it holds; a click opens its dialog.
    component SideRow: Rectangle {
        id: side
        property string icon: ""
        property string label: ""
        property string action: ""
        property string body: ""
        property bool quiet: false            // the body is a placeholder ("none yet")
        property bool clickable: true
        signal opened()
        width: parent ? parent.width : 0; height: sideColumn.height + 26
        radius: Theme.radiusSmall
        color: clickable && sideArea.containsMouse ? Theme.alpha(Theme.text, 0.05) : Theme.glassNested
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
        border.width: 1
        border.color: clickable && sideArea.containsMouse ? Theme.alpha(Theme.accent, 0.45) : Theme.border
        Rectangle { objectName: "sideRowFocusRing"; visible: side.activeFocus; anchors.fill: parent; anchors.margins: -4; radius: parent.radius + 4
                    color: "transparent"; border.width: 2; border.color: Theme.alpha(Theme.materialGlint, 0.95) }
        activeFocusOnTab: clickable
        Accessible.role: clickable ? Accessible.Button : Accessible.StaticText
        Accessible.name: label + ": " + body
        Accessible.onPressAction: if (clickable) side.opened()
        Keys.onReturnPressed: if (clickable) side.opened()
        Keys.onSpacePressed: if (clickable) side.opened()
        MouseArea { id: sideArea; anchors.fill: parent; enabled: side.clickable; hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor; onClicked: side.opened() }
        Column {
            id: sideColumn
            x: 16; y: 13; width: parent.width - 32; spacing: 6
            Item {
                width: parent.width; height: 20
                Icon { id: sideIcon; anchors.verticalCenter: parent.verticalCenter; name: side.icon; size: 16; tint: Theme.accent }
                Text { anchors.verticalCenter: parent.verticalCenter; x: sideIcon.width + 10
                       width: parent.width - x - (actionText.visible ? actionText.implicitWidth + 10 : 0); elide: Text.ElideRight
                       text: side.label; color: Theme.text; font: Theme.pageRowStrong }
                Text { id: actionText; visible: side.action !== ""; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                       text: side.action; color: Theme.accent; font: Theme.pageMeta }
            }
            Text { objectName: side.objectName + "Body"; width: parent.width; wrapMode: Text.Wrap; maximumLineCount: 3; elide: Text.ElideRight
                   textFormat: Text.PlainText; text: side.body; font: Theme.pageMeta
                   color: side.quiet ? Theme.textDim : Theme.textMuted }
        }
    }

    // -- the header: the way back, the name, its state and its actions ---------
    Column {
        id: header
        width: parent.width; spacing: 8
        // "Projeler › <name>": the way back to the card grid.
        Row {
            spacing: 6
            Text {
                objectName: "projectsBack"
                text: qsTranslate("ProjectsPage", "Projects"); color: backArea.containsMouse || activeFocus ? Theme.text : Theme.textLabel
                font.family: Theme.pageMeta.family; font.pixelSize: Theme.pageMeta.pixelSize
                font.underline: backArea.containsMouse || activeFocus
                activeFocusOnTab: true
                Accessible.role: Accessible.Link; Accessible.name: qsTranslate("ProjectsPage", "All projects")
                Accessible.onPressAction: bridge.showProject("")
                Keys.onReturnPressed: bridge.showProject("")
                Keys.onSpacePressed: bridge.showProject("")
                MouseArea { id: backArea; anchors.fill: parent; anchors.margins: -4; hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor; onClicked: bridge.showProject("") }
            }
            Icon { anchors.verticalCenter: parent.verticalCenter; name: "chevron-right"; size: 12; tint: Theme.textDim }
            Text { anchors.verticalCenter: parent.verticalCenter; width: Math.min(implicitWidth, projectPage.width / 2); elide: Text.ElideRight
                   textFormat: Text.PlainText; text: projectPage.project.name || ""; color: Theme.textMuted; font: Theme.pageMeta }
        }
        // The name (elided; its tip has it whole), its state pill, the actions.
        Item {
            width: parent.width; height: 40
            Text { id: detailName; objectName: "projectDetailName"; anchors.verticalCenter: parent.verticalCenter
                   width: Math.max(0, Math.min(implicitWidth, parent.width - statePill.width - 12 - detailActions.width - 16))
                   text: projectPage.project.name || ""; textFormat: Text.PlainText
                   elide: Text.ElideRight; color: Theme.text; font: Theme.titleLarge }
            HoverTip { objectName: "projectDetailNameTip"; target: detailName; text: detailName.text; active: detailName.truncated }
            Pill { id: statePill; objectName: "projectStatePill"; x: detailName.x + detailName.width + 12; anchors.verticalCenter: parent.verticalCenter
                   font: Theme.pageSection; implicitHeight: 24
                   text: projectPage.live ? qsTranslate("ProjectsPage", "active") : qsTranslate("ProjectsPage", "archived")
                   tint: projectPage.live ? Theme.success : Theme.warning }
            Row {
                id: detailActions
                anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; spacing: 6
                ActionButton { id: renameButton; objectName: "projectRename"; compact: true; label: qsTranslate("ProjectsPage", "Rename")
                               visible: projectPage.live && !projectPage.renaming; onClicked: projectPage.startRename() }
                ActionButton { objectName: "projectArchive"; compact: true; visible: projectPage.live; label: qsTranslate("ProjectsPage", "Archive"); danger: true
                               enabled: bridge.runningProjectId !== projectPage.project.id     // R1: not while it runs there
                               onClicked: bridge.archiveProject(projectPage.project.id) }
                ActionButton { objectName: "projectDetailUnarchive"; compact: true; visible: projectPage.project.active === false
                               label: qsTranslate("ProjectsPage", "Unarchive"); onClicked: bridge.unarchiveProject(projectPage.project.id) }
            }
        }
        Row {
            objectName: "projectRenameRow"
            visible: projectPage.renaming && projectPage.live; spacing: 8
            Field { id: renameField; objectName: "projectRenameField"; width: Math.min(320, header.width - 240); height: 36
                    placeholderText: qsTranslate("ProjectsPage", "New name"); onAccepted: projectPage.saveRename()
                    // Esc here puts the field away; it must not reach the window's Escape (leave the page).
                    Keys.onShortcutOverride: function (event) { event.accepted = event.key === Qt.Key_Escape }
                    Keys.onEscapePressed: projectPage.stopRename() }
            ActionButton { objectName: "projectRenameSave"; label: qsTranslate("ProjectsPage", "Save"); primary: true; height: 36; onClicked: projectPage.saveRename() }
            ActionButton { objectName: "projectRenameCancel"; label: qsTranslate("ProjectsPage", "Cancel"); height: 36; onClicked: projectPage.stopRename() }
        }
        Text { objectName: "projectDetailNotice"; visible: bridge.projectNotice !== ""; width: parent.width
               wrapMode: Text.Wrap; textFormat: Text.PlainText; text: bridge.projectNotice; color: Theme.warning; font: Theme.pageRow }
        Rectangle {
            objectName: "projectArchivedBanner"
            visible: projectPage.project.active === false
            width: parent.width; height: visible ? archivedText.height + 20 : 0; radius: Theme.radiusSmall
            color: Theme.alpha(Theme.warning, 0.08); border.width: 1; border.color: Theme.alpha(Theme.warning, 0.5)
            Text { id: archivedText; x: 12; y: 10; width: parent.width - 24; wrapMode: Text.Wrap; color: Theme.warning; font: Theme.pageRow
                   text: qsTranslate("ProjectsPage", "Archived: this project is read-only. Its conversations are kept and can be read, but nothing new runs in it until you unarchive it.") }
        }
    }

    // -- the right column: instruction, files and (6H) the folder ---------------
    readonly property real sideWidth: Math.round(Math.max(220, Math.min(340, width * 0.32)))
    Column {
        objectName: "projectSideRows"
        x: parent.width - width; y: header.height + 16
        width: projectPage.sideWidth; spacing: 10
        SideRow {
            objectName: "projectInstructionRow"
            icon: "pencil"; label: qsTr("Instructions"); action: projectPage.live ? qsTranslate("ProjectsPage", "Edit") : qsTr("View")
            body: (bridge.projectInstruction.text || "").trim() || qsTr("No instruction yet. Write the context every conversation in this project should carry.")
            quiet: !(bridge.projectInstruction.text || "").trim()
            onOpened: projectPage.section = "instruction"
        }
        SideRow {
            objectName: "projectFilesRow"
            readonly property int active: bridge.projectFileCounts.active || 0
            icon: "paperclip"; label: qsTr("Files"); action: projectPage.live ? qsTr("Manage") : qsTr("View")
            body: active === 0 ? qsTr("No active file") : active === 1 ? qsTr("1 active file") : qsTr("%1 active files").arg(projectPage.number(active))
            quiet: active === 0
            onOpened: projectPage.section = "files"
        }
        // 6H: the CURRENT link, or honestly none; an archived project without one has no row.
        SideRow {
            id: folderRow
            objectName: "projectFolderRow"
            readonly property bool linked: !!bridge.projectFolder.path
            visible: linked || projectPage.live
            icon: "folder"; label: qsTr("Folder")
            action: !projectPage.live ? qsTr("View") : linked ? qsTr("Manage") : qsTr("Link a folder")
            body: bridge.projectFolder.path || qsTr("No folder linked")
            quiet: !linked
            onOpened: projectPage.section = "folder"
            HoverTip { target: folderRow; text: folderRow.body; active: folderRow.visible && folderRow.linked }
        }
        // V2.1a M4: approved + pending records; the app lists only an active project's memory.
        SideRow {
            objectName: "projectMemoryRow"
            readonly property int count: memoryDialog.records.length
            readonly property int pending: memoryDialog.pending
            visible: projectPage.live
            icon: "memory"; label: qsTr("Memory"); action: qsTr("Manage")
            body: (count === 0 ? qsTr("No memory yet")
                  : count >= memoryDialog.pageSize ? qsTr("%1+ records").arg(projectPage.number(count))
                  : count === 1 ? qsTr("1 record") : qsTr("%1 records").arg(projectPage.number(count)))
                  + (pending > 0 ? "  ·  " + qsTr("%1 pending").arg(projectPage.number(pending)) : "")
            quiet: count === 0
            onOpened: projectPage.section = "memory"
        }
    }

    // -- the left column: the composer, then the conversations; one scroll -----
    Flickable {
        id: mainColumn
        objectName: "projectConversationsColumn"
        y: header.height + 16; width: parent.width - projectPage.sideWidth - 24; height: parent.height - y
        clip: true; contentHeight: conversations.height; boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar { policy: mainColumn.contentHeight > mainColumn.height ? ScrollBar.AlwaysOn : ScrollBar.AsNeeded }
        Column {
            id: conversations
            width: mainColumn.width - (mainColumn.contentHeight > mainColumn.height ? 12 : 0); spacing: 10
            // "Bu projede yeni sohbet": the one composer with its C1 row (G6SB3F).
            // Empty, its send key only opens a new conversation in this project.
            Composer {
                objectName: "projectComposer"
                width: parent.width
                readonly property string reason: projectPage.live ? qsTranslate("ProjectsPage", "New conversation in %1").arg(projectPage.project.name || "")
                                                                  : qsTranslate("ProjectsPage", "Unarchive the project first")
                conversationBound: false
                readOnly: !projectPage.live
                canSend: projectPage.live && (text.trim() === "" || !blocked)
                placeholder: reason; sendTip: reason
                inputName: "projectComposerInput"; sendName: "projectNewConversation"
                sendHandler: function (text) { return projectPage.start(text) }
                onSubmitted: bridge.setPage("home")
            }
            Text { objectName: "projectConversationNotice"; visible: text !== "" && !deleteDialog.visible; width: parent.width
                   wrapMode: Text.Wrap; textFormat: Text.PlainText; text: bridge.conversationNotice; color: Theme.warning; font: Theme.pageMeta }
            // SABİTLENEN: this project's pinned conversations, in the sidebar's pin order.
            Text { objectName: "projectPinnedHeading"; visible: pinnedRepeater.count > 0; topPadding: 6
                   text: qsTr("PINNED"); color: Theme.textLabel; font: Theme.pageSection }
            Column {
                width: parent.width; spacing: 4
                visible: pinnedRepeater.count > 0
                Repeater {
                    id: pinnedRepeater
                    model: bridge.pinnedConversations.filter(function (c) { return c.project_id === projectPage.shownId })
                    ConversationRow {
                        objectName: "projectPinnedRow"
                        width: parent.width
                        // The pin list carries no deletability; it is the open project's state.
                        row: Object.assign({ deletable: projectPage.project.active === false }, modelData)
                    }
                }
            }
            // SON SOHBETLER: the rest, newest first.
            Text { objectName: "projectConversationsHeading"; topPadding: 6; width: parent.width; elide: Text.ElideRight
                   text: qsTr("RECENT CONVERSATIONS"); color: Theme.textLabel; font: Theme.pageSection }
            Column {
                width: parent.width; spacing: 4
                Repeater {
                    model: {
                        var pinned = {}
                        bridge.pinnedConversations.forEach(function (c) { pinned[c.id] = true })
                        return bridge.projectConversations.filter(function (c) { return !pinned[c.id] })
                    }
                    ConversationRow { objectName: "projectConversationRow"; width: parent.width; row: modelData }
                }
                Text { objectName: "projectConversationsEmpty"; visible: bridge.projectConversations.length === 0
                       text: qsTranslate("ProjectsPage", "No conversation in this project yet."); color: Theme.textLabel; font: Theme.pageMeta }
            }
            // Gate 6D-b: the project's archived conversations. A row opens
            // read-only; unarchiving waits for the project's own unarchive.
            Text { objectName: "projectArchivedConversationsHeading"; visible: archivedConversationList.count > 0; topPadding: 6
                   text: qsTranslate("ProjectsPage", "ARCHIVED CONVERSATIONS"); color: Theme.textLabel; font: Theme.pageSection }
            Text { objectName: "projectConversationUnarchiveReason"; visible: archivedConversationList.count > 0 && projectPage.project.active === false
                   width: parent.width; wrapMode: Text.Wrap; color: Theme.textLabel; font: Theme.pageMeta
                   text: qsTranslate("ProjectsPage", "Unarchive the project first") }
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
                    badge: qsTranslate("ProjectsPage", "ARCHIVED"); badgeTint: Theme.warning
                    meta: qsTranslate("ProjectsPage", "archived %1").arg(projectPage.when(row.archived_at))
                    onActivated: { var b = bridge, id = archivedRow.row.id; b.openConversation(id); b.setPage("home") }
                    Reasoned {
                        reason: projectPage.live ? "" : qsTranslate("ProjectsPage", "Unarchive the project first")
                        ActionButton { objectName: "projectConversationUnarchive"; compact: true; label: qsTranslate("ProjectsPage", "Unarchive")
                                       enabled: projectPage.live && !projectPage.runsIn(archivedRow.row.id)
                                       onClicked: { var b = bridge, id = archivedRow.row.id; b.unarchiveConversation(id) } }
                    }
                    ActionButton { objectName: "projectArchivedConversationDelete"; compact: true; danger: true
                                   label: qsTranslate("ProjectsPage", "Delete permanently…"); enabled: !projectPage.runsIn(archivedRow.row.id)
                                   onClicked: deleteDialog.openFor(archivedRow.row.id) }
                }
            }
        }
    }

    ConversationDeleteDialog { id: deleteDialog }

    // -- Talimatlar: the existing instruction editor, in a dialog --------------
    // Guidance for the model, never authority. The store decides (2,000-character
    // cap, PUBLIC/INTERNAL, archive refusal) and redacts; what it stored is what
    // this editor shows after a save.
    JarvisDialog {
        id: instructionDialog
        objectName: "projectInstructionDialog"
        label: qsTr("Instructions")
        title: projectPage.project.name || ""
        preferredWidth: 720
        onClosed: if (projectPage.section === "instruction") projectPage.section = "conversations"
        Column {
            objectName: "instructionPanel"
            width: parent ? parent.width : 0; spacing: 8
            Text { objectName: "instructionPrecedence"; width: parent.width; wrapMode: Text.Wrap; color: Theme.textMuted; font: Theme.pageMeta
                   text: qsTranslate("ProjectsPage", "Order: JARVIS safety rules and Core decisions, then your current message, then this instruction, then retrieved memory. The instruction guides the model; it never grants a permission or approves anything.") }
            Rectangle {
                width: parent.width; height: 240; radius: 6
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
                        readOnly: !projectPage.live
                        color: Theme.text; font: Theme.body
                        EditMenu { objectName: "instructionEditorMenu"; editor: instructionEditor }
                    }
                }
            }
            Row {
                spacing: 8
                Text { objectName: "instructionCounter"; anchors.verticalCenter: parent.verticalCenter; width: 110
                       text: qsTranslate("ProjectsPage", "%1 / %2").arg(instructionEditor.length).arg(bridge.projectInstructionCap)
                       color: instructionEditor.length > bridge.projectInstructionCap ? Theme.warning : Theme.textMuted; font: Theme.pageMetaFigures }
                Text { anchors.verticalCenter: parent.verticalCenter; text: qsTranslate("ProjectsPage", "Classification:"); color: Theme.textLabel; font: Theme.pageMeta }
                Repeater {
                    model: ["public", "internal"]
                    delegate: Choice { objectName: "instructionClass_" + modelData; value: modelData; enabled: projectPage.live
                                       selected: projectPage.instructionClass === modelData; onPicked: projectPage.instructionClass = modelData }
                }
                Reasoned {
                    reason: instructionReason.text
                    ActionButton { objectName: "instructionClear"; compact: true; label: qsTranslate("ProjectsPage", "Clear")
                                   enabled: projectPage.live && projectPage.instructionClass !== ""
                                   onClicked: bridge.saveProjectInstruction(projectPage.project.id, "", projectPage.instructionClass) }
                }
                Reasoned {
                    reason: instructionReason.text
                    ActionButton { objectName: "instructionSave"; compact: true; primary: true; label: qsTranslate("ProjectsPage", "Save")
                                   enabled: projectPage.live && projectPage.instructionClass !== ""
                                   onClicked: bridge.saveProjectInstruction(projectPage.project.id, instructionEditor.text, projectPage.instructionClass) }
                }
            }
            Text { id: instructionReason; objectName: "instructionReason"; width: parent.width; wrapMode: Text.Wrap; font: Theme.pageMeta
                   visible: text !== ""; color: projectPage.live ? Theme.textMuted : Theme.warning
                   text: !projectPage.live ? qsTranslate("ProjectsPage", "Archived project: the instruction is read-only.")
                         : projectPage.instructionClass === "" ? qsTranslate("ProjectsPage", "Choose PUBLIC or INTERNAL before saving.") : "" }
            Text { objectName: "instructionNotice"; width: parent.width; wrapMode: Text.Wrap; visible: text !== ""
                   textFormat: Text.PlainText; text: bridge.projectInstructionNotice; color: Theme.warning; font: Theme.pageMeta }
        }
        buttons: [ ActionButton { objectName: "instructionDialogClose"; label: qsTr("Close"); height: 36; width: 110; onClicked: instructionDialog.close() } ]
    }

    // -- Klasör (Gate 6H): the folder link, in a dialog ---------------------------
    // CONTEXT for the person only (Salih Q1): JARVIS never reads, writes or sends
    // the folder. "Browse…" picks an EXISTING folder; its exact path shows before
    // "Link". Unlink ends the link only - the folder is never deleted. The backend
    // decides (reparse points, protected roots, overlap) and its reason is shown.
    property string pickedFolder: ""            // the exact path "Link" sends ("" = none picked)
    JarvisDialog {
        id: folderDialog
        objectName: "projectFolderDialog"
        label: qsTr("Folder")
        title: projectPage.project.name || ""
        preferredWidth: 640
        readonly property bool linked: !!bridge.projectFolder.path
        onClosed: { projectPage.pickedFolder = ""; if (projectPage.section === "folder") projectPage.section = "conversations" }
        Text { objectName: "folderContextNote"; width: parent ? parent.width : 0; wrapMode: Text.Wrap; color: Theme.textMuted; font: Theme.pageMeta
               text: qsTr("A linked folder is context for you only: JARVIS does not read it, write into it or send it to the model.") }
        Rectangle {
            objectName: "folderPathCard"
            width: parent ? parent.width : 0; height: folderPathColumn.height + 20; radius: Theme.radiusSmall
            color: Theme.glassNested; border.width: 1; border.color: Theme.alpha(Theme.text, 0.10)
            Column {
                id: folderPathColumn
                x: 12; y: 10; width: parent.width - 24; spacing: 4
                Text { objectName: "folderPathLabel"; text: folderDialog.linked ? qsTr("Linked folder") : projectPage.pickedFolder !== "" ? qsTr("Selected folder") : qsTr("No folder linked")
                       color: Theme.textLabel; font: Theme.pageMeta }
                Text { objectName: "folderPath"; width: parent.width; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText; color: Theme.text; font: Theme.pageRowStrong
                       visible: text !== ""; text: bridge.projectFolder.path || projectPage.pickedFolder }
                Text { objectName: "folderLinkedAt"; visible: folderDialog.linked; color: Theme.textMuted; font: Theme.pageMeta
                       text: qsTr("linked %1").arg(projectPage.when(bridge.projectFolder.linked_at)) }
            }
        }
        Text { objectName: "folderUnlinkNote"; visible: folderDialog.linked && projectPage.live; width: parent ? parent.width : 0; wrapMode: Text.Wrap
               color: Theme.textMuted; font: Theme.pageMeta
               text: qsTr("Unlinking only ends the link to this project: the folder is not deleted and nothing in it changes.") }
        Text { objectName: "folderReason"; visible: !projectPage.live; width: parent ? parent.width : 0; wrapMode: Text.Wrap
               color: Theme.warning; font: Theme.pageMeta; text: qsTr("Archived project: the folder link cannot change.") }
        Text { objectName: "folderNotice"; width: parent ? parent.width : 0; wrapMode: Text.Wrap; visible: text !== ""
               textFormat: Text.PlainText; text: bridge.projectFolderNotice; color: Theme.warning; font: Theme.pageMeta }
        buttons: [
            ActionButton { objectName: "projectFolderBrowse"; label: qsTr("Browse…"); height: 36; visible: projectPage.live && !folderDialog.linked
                           onClicked: folderPicker.open() },
            ActionButton { objectName: "projectFolderLink"; label: qsTr("Link"); primary: true; height: 36; width: 110
                           visible: projectPage.live && !folderDialog.linked; enabled: projectPage.pickedFolder !== ""
                           onClicked: { if (bridge.linkProjectFolder(projectPage.project.id, projectPage.pickedFolder)) projectPage.pickedFolder = "" } },
            ActionButton { objectName: "projectFolderUnlink"; label: qsTr("Unlink"); height: 36; visible: projectPage.live && folderDialog.linked
                           onClicked: bridge.unlinkProjectFolder(projectPage.project.id) },
            ActionButton { objectName: "folderDialogClose"; label: qsTr("Close"); height: 36; width: 110; onClicked: folderDialog.close() }
        ]
    }
    // V2.1a M4: the project's memory; it also feeds the row's count.
    ProjectMemoryDialog {
        id: memoryDialog
        objectName: "projectMemoryDialog"
        projectId: projectPage.live ? projectPage.shownId : ""
        title: projectPage.project.name || ""
        host: projectPage
        onClosed: if (projectPage.section === "memory") projectPage.section = "conversations"
    }
    FolderDialog {
        id: folderPicker
        objectName: "projectFolderPicker"
        onAccepted: projectPage.pickedFolder = bridge.folderPath(selectedFolder)
    }

    // -- Dosyalar: the existing file panel, in a dialog -------------------------
    // Files are untrusted data, never instructions. Counters come from the
    // store's listing; its refusals are shown as returned. Purge hands the
    // screen to its own dialog and comes back here after it (one scrim at a time).
    property bool handingOver: false
    property Item filesOpener: null
    function openPurge(file, opener) {
        handingOver = true
        filesDialog.opener = null              // no focus return: the purge dialog takes it
        filesDialog.close()
        handingOver = false
        purgeDialog.opener = opener
        purgeDialog.openFor(file)
    }
    JarvisDialog {
        id: filesDialog
        objectName: "projectFilesDialog"
        label: qsTr("Files")
        title: projectPage.project.name || ""
        preferredWidth: 760
        exit: projectPage.handingOver || !moves ? null : exitMotion
        onOpened: projectPage.filesOpener = opener
        onClosed: if (projectPage.section === "files" && !projectPage.handingOver) projectPage.section = "conversations"
        Column {
            objectName: "filePanel"
            width: parent ? parent.width : 0; spacing: 8
            readonly property var counts: bridge.projectFileCounts
            Row {
                spacing: 18
                Text { objectName: "fileCountActive"; color: Theme.textLabel; font: Theme.pageMetaFigures
                       text: qsTranslate("ProjectsPage", "Active %1/%2").arg(projectPage.number(parent.parent.counts.active)).arg(projectPage.number(parent.parent.counts.active_max)) }
                Text { objectName: "fileCountRetained"; color: Theme.textLabel; font: Theme.pageMetaFigures
                       text: qsTranslate("ProjectsPage", "Retained %1/%2").arg(projectPage.number(parent.parent.counts.retained)).arg(projectPage.number(parent.parent.counts.retained_max)) }
                Text { objectName: "fileCountChars"; color: Theme.textLabel; font: Theme.pageMetaFigures
                       text: qsTranslate("ProjectsPage", "Characters %1/%2").arg(projectPage.number(parent.parent.counts.chars)).arg(projectPage.number(parent.parent.counts.chars_max)) }
            }
            Row {
                spacing: 8
                Text { anchors.verticalCenter: parent.verticalCenter; text: qsTranslate("ProjectsPage", "Classification:"); color: Theme.textLabel; font: Theme.pageMeta }
                Repeater {
                    model: ["public", "internal", "sensitive", "secret"]
                    delegate: Choice { objectName: "fileClass_" + modelData; value: modelData; enabled: projectPage.live
                                       selected: projectPage.fileClass === modelData; onPicked: projectPage.fileClass = modelData }
                }
                Reasoned {
                    reason: fileReason.text
                    ActionButton { objectName: "fileAdd"; compact: true; primary: true; label: qsTranslate("ProjectsPage", "Add file…")
                                   enabled: projectPage.live && projectPage.fileClass !== ""
                                   onClicked: filePicker.open() }
                }
            }
            Text { id: fileReason; objectName: "fileReason"; width: parent.width; wrapMode: Text.Wrap; font: Theme.pageMeta
                   visible: text !== ""; color: projectPage.live ? Theme.textMuted : Theme.warning
                   text: !projectPage.live ? qsTranslate("ProjectsPage", "Archived project: files cannot be added, retired or restored. Purge stays available.")
                         : projectPage.fileClass === "" ? qsTranslate("ProjectsPage", "Choose a classification before adding a file. SECRET files are stored but never retrieved.") : "" }
            Text { objectName: "fileNotice"; width: parent.width; wrapMode: Text.Wrap; visible: text !== ""
                   textFormat: Text.PlainText; text: bridge.projectFileNotice; color: Theme.warning; font: Theme.pageMeta }
            Column {
                objectName: "fileList"
                width: parent.width; spacing: 4
                Repeater {
                    model: bridge.projectFiles
                    delegate: Rectangle {
                        objectName: "fileRow"
                        property var file: modelData
                        width: parent ? parent.width : 0; height: 52; radius: Theme.radiusSmall; color: "transparent"
                        border.width: 1; border.color: Theme.border
                        Column {
                            anchors { left: parent.left; leftMargin: 10; right: fileButtons.left; rightMargin: 10; verticalCenter: parent.verticalCenter }
                            spacing: 2
                            Text { objectName: "fileRowName"; width: parent.width; text: file.display_name || ""; textFormat: Text.PlainText
                                   elide: Text.ElideRight; color: file.lifecycle === "active" ? Theme.text : Theme.textMuted; font: Theme.pageRowStrong }
                            Text { objectName: "fileRowMeta"; width: parent.width; textFormat: Text.PlainText; elide: Text.ElideRight
                                   color: Theme.textMuted; font: Theme.pageMeta; text: projectPage.fileMeta(file) }
                        }
                        Row {
                            id: fileButtons
                            anchors { right: parent.right; rightMargin: 8; verticalCenter: parent.verticalCenter }
                            spacing: 6
                            ActionButton { objectName: "fileRetire"; compact: true; label: qsTranslate("ProjectsPage", "Retire"); visible: file.lifecycle === "active"
                                           enabled: projectPage.live; onClicked: bridge.retireProjectFile(projectPage.project.id, file.id) }
                            ActionButton { objectName: "fileRestore"; compact: true; label: qsTranslate("ProjectsPage", "Restore"); visible: file.lifecycle === "retired"
                                           enabled: projectPage.live; onClicked: bridge.restoreProjectFile(projectPage.project.id, file.id) }
                            ActionButton { id: purgeButton; objectName: "filePurge"; compact: true; danger: true; label: qsTranslate("ProjectsPage", "Purge…")
                                           visible: file.purgeable === true; onClicked: projectPage.openPurge(file, purgeButton) }
                        }
                    }
                }
                Text { visible: bridge.projectFiles.length === 0; text: qsTranslate("ProjectsPage", "No file in this project yet.")
                       color: Theme.textMuted; font: Theme.pageMeta }
            }
        }
        buttons: [ ActionButton { objectName: "filesDialogClose"; label: qsTr("Close"); height: 36; width: 110; onClicked: filesDialog.close() } ]
    }

    FileDialog {
        id: filePicker
        objectName: "projectFileDialog"
        fileMode: FileDialog.OpenFile
        nameFilters: [qsTranslate("ProjectsPage", "Text and code (%1)").arg("*.txt *.md *.json *.yaml *.yml *.csv *.log *.py *.js *.ts *.qml *.html *.css *.xml *.sh *.sql")]
        onAccepted: bridge.addProjectFile(projectPage.project.id, selectedFile, projectPage.fileClass)
    }

    // Purge: the permanent second step. The dialog names the exact row (names
    // are not unique), says honestly what purge does not reach (ARCHITECTURE
    // 4.24) and acts only when that row's file name is typed exactly.
    // Gate 6E: on the shared dialog shell (danger tone; focus starts in the
    // name field, where Enter confirms nothing). Closed, it gives the screen
    // back to the file dialog it came from, with focus on the row's Purge.
    JarvisDialog {
        id: purgeDialog
        objectName: "purgeDialog"
        property var file: ({})
        danger: true
        preferredWidth: 560
        label: qsTranslate("ProjectsPage", "Purge")
        title: qsTranslate("ProjectsPage", "Purge this file permanently?")
        initialFocus: confirmName
        confirmItem: purgeConfirm
        confirmEnabled: confirmName.text === (purgeDialog.file.display_name || null)
        enterGuard: [confirmName]
        function openFor(row) { file = row; confirmName.text = ""; open() }
        // The Purge key of the row it was opened for (null once that row is gone).
        property Item from: null
        onAboutToShow: from = opener
        onClosed: {
            if (projectPage.section !== "files" || projectPage.shownId === "") return
            filesDialog.opener = projectPage.filesOpener
            filesDialog.open()
            if (from && from.visible) from.forceActiveFocus()
        }
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
                       color: Theme.textMuted; font: Theme.pageMeta; text: projectPage.fileMeta(purgeDialog.file) }
            }
        }
        Text { objectName: "purgeResidue"; width: parent ? parent.width : 0; wrapMode: Text.Wrap; color: Theme.textLabel; font: Theme.pageMeta
               text: qsTranslate("ProjectsPage", "Purge deletes this file's text from the project store for good and frees its quota; a tombstone (id, fingerprint, time) remains. It does NOT reach: copies already sent to a provider, approval previews and audit records that showed excerpts, task results and conversation history, database backups (such as jarvis.pre-v2.db), or leftover data in database free pages, the WAL, the file system or the disk.") }
        Text { width: parent ? parent.width : 0; wrapMode: Text.Wrap; textFormat: Text.PlainText; color: Theme.text; font: Theme.pageMeta
               text: qsTranslate("ProjectsPage", "Type the file name to confirm: %1").arg(purgeDialog.file.display_name || "") }
        Field { id: confirmName; objectName: "purgeConfirmName"; width: parent ? parent.width : 0; height: 36; maximumLength: 1024 }
        buttons: [
            ActionButton { objectName: "purgeCancel"; label: qsTranslate("ProjectsPage", "Cancel"); height: 36; width: 110; onClicked: purgeDialog.close() },
            ActionButton { id: purgeConfirm; objectName: "purgeConfirm"; label: qsTranslate("ProjectsPage", "Purge"); danger: true; primary: true; height: 36; width: 110
                           onClicked: { if (bridge.purgeProjectFile(projectPage.project.id, purgeDialog.file.id, confirmName.text)) purgeDialog.close() } }
        ]
    }
}
