import QtQuick
import QtQuick.Controls.Basic
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

    component Field: TextField {
        selectByMouse: true; color: Theme.text; font: Theme.body; maximumLength: 120
        placeholderTextColor: Theme.textDim
        background: Rectangle { radius: 6; color: Theme.surfaceDeep; border.width: 1
                                border.color: parent.activeFocus ? Theme.commandEdge : Theme.border }
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
                Item { width: parent.width; height: 36
                    Text { anchors.verticalCenter: parent.verticalCenter; text: qsTr("CONVERSATIONS"); color: Theme.textLabel; font: Theme.pageSection }
                    ActionButton { objectName: "projectNewConversation"; anchors.right: parent.right; compact: true
                                   enabled: page.current.active === true
                                   label: qsTr("New conversation in %1").arg(page.current.name || "")
                                   onClicked: { var b = bridge; if (b.newProjectConversation(page.current.id)) b.setPage("home") } } }
                ListView {
                    id: conversationList
                    objectName: "projectConversationList"
                    width: parent.width; height: Math.min(Math.max(contentHeight, 40), parent.height * 0.42); clip: true; spacing: 4
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
        }
    }
}
