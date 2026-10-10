import QtQuick
import QtQuick.Controls.Basic
import "../theme"
import "../components"
import "../overlays"

// Projects (V2.0): the window's view of the project model. Names are shown;
// ids stay internal. Every change goes through the existing explicit-user
// paths and Core's answer is shown as returned. A project is context, never
// authority: nothing here approves, grants or selects anything.
SecondaryPage {
    id: page
    objectName: "projectsPage"
    title: qsTr("Projects")
    headerRight: [ ActionButton { label: qsTr("Refresh"); onClicked: bridge.refreshProjects() } ]
    readonly property var current: bridge.openProject
    // Gate 6G P3: which of the open project's dialogs shows ("conversations" =
    // none, "instruction", "files"); the card menu's Edit opens the instruction.
    property alias section: projectPage.section
    // Gate 6G P2: the card grid's tab, name search and sort. Last activity is the
    // project's newest live conversation, else its creation (never invented).
    property string tab: "mine"             // "mine" | "archived"
    property string query: ""
    property string sort: "activity"        // "activity" (default) | "name"
    function activityAt(p) {
        var rows = bridge.sidebarConversations[p.id] || []
        var at = rows.length ? Date.parse(rows[0].updated_at) : NaN
        return isNaN(at) || at < Date.parse(p.created_at) ? p.created_at || "" : rows[0].updated_at
    }
    // Gate 6G P2 pins: the sidebar's own project pins (one list, newest pin
    // first); on "Your projects" the pinned cards lead in that order, then the sort.
    readonly property var pinned: bridge.sidebarTree.pinned_projects || []
    readonly property var cards: {
        var q = query.trim().toLowerCase(), mine = tab === "mine"
        var out = bridge.projectRecords.filter(function (p) {
            return (p.active === true) === mine && (q === "" || String(p.name).toLowerCase().indexOf(q) >= 0)
        }).map(function (p) { return { project: p, at: page.activityAt(p), pinned: mine && page.pinned.indexOf(p.id) >= 0 } })
        out.sort(sort === "name" ? function (a, b) { return String(a.project.name).localeCompare(String(b.project.name)) }
                                 : function (a, b) { return (Date.parse(b.at) || 0) - (Date.parse(a.at) || 0) })
        return out.filter(function (c) { return c.pinned }).sort(function (a, b) {
            return page.pinned.indexOf(a.project.id) - page.pinned.indexOf(b.project.id)
        }).concat(out.filter(function (c) { return !c.pinned }))
    }
    function openCard(id, section) { page.section = section; bridge.showProject(id) }
    // Gate 6F (Q9): the sidebar's PROJELER "+" lands here; Gate 6G P1: on the new-project popup.
    function focusCreate() { newProjectDialog.openDialog() }

    Item {
        anchors.fill: parent

        // -- Gate 6G P2: the card grid (no project open) --------------------------
        Item {
            id: gridPane
            objectName: "projectGridPane"
            visible: !page.current.id
            anchors.fill: parent
            // Tabs at the left; search, sort and "New project" at the right (the search gives way first).
            Item {
                id: toolbar
                width: parent.width; height: 36
                Segmented { id: tabs; objectName: "projectsTabs"; segmentPrefix: "projectsTab_"; anchors.verticalCenter: parent.verticalCenter
                            options: [{ value: "mine", label: qsTr("Your projects") }, { value: "archived", label: qsTr("Archived") }]
                            value: page.tab; onPicked: function (value) { page.tab = value } }
                Row {
                    id: tools
                    anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; spacing: 8
                    ProjectPage.Field { id: search; objectName: "projectsSearch"; height: 34; placeholderText: qsTr("Search projects")
                                        width: Math.max(120, Math.min(240, toolbar.width - tabs.width - sortChoice.width - newProject.width - 3 * 8 - 24))
                                        onTextChanged: page.query = text }
                    Segmented { id: sortChoice; objectName: "projectsSort"; segmentPrefix: "projectsSort_"; anchors.verticalCenter: parent.verticalCenter
                                options: [{ value: "activity", label: qsTr("Last activity") }, { value: "name", label: qsTr("Name") }]
                                value: page.sort; onPicked: function (value) { page.sort = value } }
                    ActionButton { id: newProject; objectName: "projectsNew"; label: qsTr("New project"); primary: true; height: 34
                                   onClicked: newProjectDialog.openDialog() }
                }
            }
            Text { id: notice; objectName: "projectNotice"; visible: bridge.projectNotice !== ""; y: toolbar.height + 10; width: parent.width
                   wrapMode: Text.Wrap; textFormat: Text.PlainText; text: bridge.projectNotice; color: Theme.warning; font: Theme.pageRow }
            // Gate 6E (D23): compact lines at the top, never floating mid-void.
            Text { objectName: "projectsEmpty"; visible: page.cards.length === 0; y: grid.y; width: parent.width; wrapMode: Text.Wrap
                   text: bridge.projectRecords.length === 0 ? qsTr("No project yet. Use New project to create one.")
                         : page.query.trim() !== "" ? qsTr("No project matches your search.")
                         : page.tab === "archived" ? qsTr("No archived project.") : qsTr("No active project yet.")
                   color: Theme.textLabel; font: Theme.pageRow }
            GridView {
                id: grid
                objectName: "projectGrid"
                y: (notice.visible ? notice.y + notice.height : toolbar.height) + 14
                width: parent.width; height: parent.height - y
                // As many columns of at least 280 px as fit; the cards share the width exactly.
                readonly property int columns: Math.max(1, Math.floor((width + Theme.gap) / (280 + Theme.gap)))
                cellWidth: Math.floor((width + Theme.gap) / columns); cellHeight: 128 + Theme.gap
                clip: true; boundsBehavior: Flickable.StopAtBounds
                model: page.cards
                ScrollBar.vertical: ScrollBar { policy: grid.contentHeight > grid.height ? ScrollBar.AlwaysOn : ScrollBar.AsNeeded }
                delegate: Item {
                    width: grid.cellWidth; height: grid.cellHeight
                    ProjectCard {
                        width: parent.width - Theme.gap; height: 128
                        project: modelData.project; lastActivity: page.when(modelData.at); pinned: modelData.pinned
                        // Locals: opening rebuilds the model, and this delegate's context with it.
                        onOpened: { var id = project.id; page.openCard(id, "conversations") }
                        onMenuRequested: function (anchor) { projectMenu.openFor(project, anchor) }
                    }
                }
            }
        }

        // -- Gate 6G P3: the open project's page ----------------------------------
        ProjectPage {
            id: projectPage
            visible: !!page.current.id
            anchors.fill: parent
            project: page.current
            host: page
        }
    }

    NewProjectDialog {
        id: newProjectDialog
        // G6SB3F: closed with no project created (cancel, a refusal, a create still
        // in flight): a New chat that asked for it keeps its choice. On success the
        // dialog's showProject(id) hands the new project to that chat instead.
        onAboutToHide: if (waiting || failure !== "" || !sent.name) bridge.dropNewProjectChat()
    }
    Component.onDestruction: if (bridge) bridge.dropNewProjectChat()     // the page left with the dialog open

    // Gate 6G P2: the card's menu (K4). Pin keeps an active project's card (and
    // its sidebar row) on top; Edit opens the project on its instruction
    // (rename is beside it); archive is the reversible step. Projects have no
    // delete path (6D-b), so there is no delete here.
    MenuSurface {
        id: projectMenu
        objectName: "projectCardMenuSurface"
        width: 240
        property var project: ({})
        readonly property bool pinned: page.pinned.indexOf(project.id) >= 0
        function openFor(p, anchor) {
            project = p
            popupAt(anchor, Qt.point(anchor.width - width, anchor.height + 4), { trigger: anchor, flip: true })
        }
        Column {
            width: projectMenu.availableWidth
            spacing: 2
            MenuRow { objectName: "projectMenuPin"; icon: "pin"; label: projectMenu.pinned ? qsTr("Unpin") : qsTr("Pin")
                      visible: projectMenu.project.active === true
                      onTriggered: { var id = projectMenu.project.id, on = !projectMenu.pinned; projectMenu.close(); bridge.setProjectPinned(id, on) } }
            MenuRow { objectName: "projectMenuEdit"; icon: "pencil"; label: qsTr("Edit"); visible: projectMenu.project.active === true
                      onTriggered: { var id = projectMenu.project.id; projectMenu.close(); page.openCard(id, "instruction") } }
            Item { width: parent.width; height: 9; visible: projectMenu.project.active === true
                   Rectangle { y: 4; x: 8; width: parent.width - 16; height: 1; color: Theme.border } }
            MenuRow { objectName: "projectMenuArchive"; icon: "archive"; label: qsTr("Archive"); visible: projectMenu.project.active === true
                      available: bridge.runningProjectId !== projectMenu.project.id
                      onTriggered: { var id = projectMenu.project.id; projectMenu.close(); bridge.archiveProject(id) } }
            MenuRow { objectName: "projectMenuUnarchive"; icon: "archive"; label: qsTr("Unarchive"); visible: projectMenu.project.active === false
                      onTriggered: { var id = projectMenu.project.id; projectMenu.close(); bridge.unarchiveProject(id) } }
        }
    }
}
