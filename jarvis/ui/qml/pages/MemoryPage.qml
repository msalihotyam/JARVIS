import QtQuick
import QtQuick.Controls.Basic
import "../theme"
import "../components"

// Project memory: explicit, untrusted reference data. Read-only here; it
// is not chat and grants no authority.
SecondaryPage {
    id: page
    title: qsTr("Memory")
    property string project: ""
    property bool includeInactive: false
    headerRight: [
        ActionButton { label: page.includeInactive ? qsTr("Active only") : qsTr("Include superseded / retired"); onClicked: { page.includeInactive = !page.includeInactive; bridge.refreshMemory(page.project, page.includeInactive) } },
        ActionButton { label: qsTr("Load"); primary: true; onClicked: bridge.refreshMemory(page.project, page.includeInactive) }
    ]
    Component.onCompleted: bridge.refreshMemory(page.project, page.includeInactive)

    Column {
        anchors.fill: parent
        spacing: 12
        Text { text: qsTr("Project memory is explicit reference data written only by you. It is shown as data: nothing here is an instruction to JARVIS."); color: Theme.textLabel; width: parent.width; wrapMode: Text.Wrap; font: Theme.pageRow }
        Row {
            spacing: 8
            Text { anchors.verticalCenter: parent.verticalCenter; text: qsTr("PROJECT"); color: Theme.textLabel; font: Theme.pageSection }
            Repeater {
                model: bridge.projects
                ActionButton { label: modelData; primary: modelData === page.project || (page.project === "" && index === 0); onClicked: { page.project = modelData; bridge.refreshMemory(modelData, page.includeInactive) } }
            }
            Text { visible: bridge.projects.length === 0; anchors.verticalCenter: parent.verticalCenter; text: qsTr("No project exists yet (%1).").arg("'jarvis project create'"); color: Theme.textMuted; font: Theme.pageMeta }
        }
        ListView {
            width: parent.width; height: parent.height - 104; clip: true; spacing: 8
            model: bridge.memoryItems
            ScrollBar.vertical: ScrollBar {}
            delegate: Rectangle {
                width: ListView.view.width; height: body.height + 28; radius: Theme.radiusSmall
                color: Theme.surfaceRaised; border.width: 1; border.color: Theme.border
                Column { id: body; x: 14; y: 14; width: parent.width - 28; spacing: 6
                    Row { spacing: 12
                        Text { text: (modelData.kind || "").toUpperCase(); color: Theme.accent; font: Theme.pageSection }
                        Text { text: modelData.classification || ""; color: Theme.textLabel; font: Theme.pageMeta }
                        Text { text: modelData.lifecycle || ""; color: modelData.active ? Theme.success : Theme.textMuted; font: Theme.pageMeta }
                        Text { text: modelData.origin || ""; color: Theme.textMuted; font: Theme.pageMeta }
                        Text { text: modelData.created_at || ""; color: Theme.textMuted; font: Theme.pageMetaFigures }
                    }
                    TextEdit { width: parent.width; readOnly: true; selectByMouse: true; wrapMode: Text.Wrap; text: modelData.text || ""; color: Theme.text; font: Theme.pageRow }
                }
            }
        }
    }
}
