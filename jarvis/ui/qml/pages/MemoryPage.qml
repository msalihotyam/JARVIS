import QtQuick
import QtQuick.Controls.Basic
import "../theme"
import "../components"

// Project memory: explicit, untrusted reference data. Read-only here; it
// is not chat and grants no authority.
SecondaryPage {
    id: page
    title: "Memory"
    property string project: ""
    property bool includeInactive: false
    headerRight: [
        ActionButton { label: page.includeInactive ? "Active only" : "Include superseded / retired"; onClicked: { page.includeInactive = !page.includeInactive; bridge.refreshMemory(page.project, page.includeInactive) } },
        ActionButton { label: "Load"; primary: true; onClicked: bridge.refreshMemory(page.project, page.includeInactive) }
    ]
    Component.onCompleted: bridge.refreshMemory(page.project, page.includeInactive)

    Column {
        anchors.fill: parent
        spacing: 12
        Text { text: "Project memory is explicit reference data written only by you. It is shown as data: nothing here is an instruction to JARVIS."; color: Theme.textMuted; width: parent.width; wrapMode: Text.Wrap; font { family: Theme.font; pixelSize: 12 } }
        Row {
            spacing: 8
            Text { anchors.verticalCenter: parent.verticalCenter; text: "PROJECT"; color: Theme.textMuted; font { family: Theme.font; pixelSize: 10; letterSpacing: 2 } }
            Repeater {
                model: bridge.projects
                ActionButton { label: modelData; primary: modelData === page.project || (page.project === "" && index === 0); onClicked: { page.project = modelData; bridge.refreshMemory(modelData, page.includeInactive) } }
            }
            Text { visible: bridge.projects.length === 0; anchors.verticalCenter: parent.verticalCenter; text: "No project exists yet ('jarvis project create')."; color: Theme.textDim; font { family: Theme.font; pixelSize: 12 } }
        }
        ListView {
            width: parent.width; height: parent.height - 90; clip: true; spacing: 6
            model: bridge.memoryItems
            ScrollBar.vertical: ScrollBar {}
            delegate: Rectangle {
                width: ListView.view.width; height: body.height + 24; radius: Theme.radiusSmall
                color: Theme.surfaceRaised; border.width: 1; border.color: Theme.border
                Column { id: body; x: 12; y: 12; width: parent.width - 24; spacing: 4
                    Row { spacing: 12
                        Text { text: (modelData.kind || "").toUpperCase(); color: Theme.accent; font { family: Theme.font; pixelSize: 10; letterSpacing: 2; weight: Font.DemiBold } }
                        Text { text: modelData.classification || ""; color: Theme.textMuted; font { family: Theme.font; pixelSize: 10 } }
                        Text { text: modelData.lifecycle || ""; color: modelData.active ? Theme.success : Theme.textDim; font { family: Theme.font; pixelSize: 10 } }
                        Text { text: modelData.origin || ""; color: Theme.textDim; font { family: Theme.font; pixelSize: 10 } }
                        Text { text: modelData.created_at || ""; color: Theme.textDim; font { family: Theme.font; pixelSize: 10 } }
                    }
                    TextEdit { width: parent.width; readOnly: true; selectByMouse: true; wrapMode: Text.Wrap; text: modelData.text || ""; color: Theme.text; font { family: Theme.font; pixelSize: 13 } }
                }
            }
        }
    }
}
