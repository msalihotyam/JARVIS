import QtQuick
import QtQuick.Controls.Basic
import "../theme"
import "../components"

// The existing scheduler operator surface: list, enable / disable /
// archive on the revision the listing printed, create from a JSON spec,
// and one bounded foreground sweep.
SecondaryPage {
    id: page
    title: "Automations"
    property bool includeArchived: false
    property var selected: null
    signal confirmArchive(var job)
    headerRight: [
        ActionButton { label: page.includeArchived ? "Hide archived" : "Show archived"; onClicked: { page.includeArchived = !page.includeArchived; bridge.refreshJobs(page.includeArchived) } },
        ActionButton { label: "Refresh"; onClicked: bridge.refreshJobs(page.includeArchived) },
        ActionButton { label: "Run due jobs once"; primary: true; enabled: !bridge.busy && !bridge.configRefused; onClicked: { bridge.runSweep(); bridge.setPage("home") } }
    ]

    Flickable {
        id: viewport
        objectName: "automationsViewport"
        anchors.fill: parent
        contentWidth: width
        contentHeight: form.height + Theme.pad
        clip: true; boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
        Column {
        id: form
        width: viewport.width
        spacing: 12
        Row {
            id: jobActions
            spacing: 8
            property bool actionable: page.selected !== null && page.selected.readable === true && !page.selected.archived
            ActionButton { label: "Enable"; enabled: parent.actionable && !page.selected.enabled; onClicked: bridge.setJobEnabled(page.selected.id, page.selected.revision, true) }
            ActionButton { label: "Disable"; enabled: parent.actionable && page.selected.enabled === true; onClicked: bridge.setJobEnabled(page.selected.id, page.selected.revision, false) }
            ActionButton { label: "Archive"; danger: true; enabled: parent.actionable; onClicked: page.confirmArchive(page.selected) }
            Text { anchors.verticalCenter: parent.verticalCenter; text: "Enable / disable / archive act on the revision this listing shows."; color: Theme.textDim; font { family: Theme.font; pixelSize: 11 } }
        }
        Item {
            id: jobListing
            width: parent.width
            height: bridge.jobs.length === 0 ? 104 : Math.max(132, Math.min(240, viewport.height * 0.32))
            Rectangle {
                objectName: "automationsEmpty"
                anchors.fill: parent; visible: bridge.jobs.length === 0
                radius: Theme.radiusSmall; color: Theme.surfaceDeep; border.width: 1; border.color: Theme.border
                Row { anchors.centerIn: parent; spacing: 16
                    IconBox { width: 38; height: 38; glyph: "⟳"; tint: Theme.accent }
                    Column { anchors.verticalCenter: parent.verticalCenter; spacing: 6
                        Text { text: "No automations yet"; color: Theme.text; font { family: Theme.font; pixelSize: 15; weight: Font.DemiBold } }
                        Text { text: "Create a job below, or ask JARVIS from the Command Center."; color: Theme.textMuted; font { family: Theme.font; pixelSize: 12 } }
                    }
                }
            }
            ListView {
            anchors.fill: parent; visible: bridge.jobs.length > 0; clip: true; spacing: 4
            model: bridge.jobs
            ScrollBar.vertical: ScrollBar {}
            delegate: Rectangle {
                width: ListView.view.width; height: 46; radius: Theme.radiusSmall
                property bool current: page.selected !== null && page.selected.id === modelData.id
                color: current ? Qt.rgba(Theme.accent.r, Theme.accent.g, Theme.accent.b, 0.14) : "transparent"
                border.width: 1; border.color: current ? Theme.accent : Theme.border
                Row { anchors.fill: parent; anchors.margins: 10; spacing: 16
                    Rectangle { width: 8; height: 8; radius: 4; anchors.verticalCenter: parent.verticalCenter; color: modelData.archived ? Theme.textDim : (modelData.enabled ? Theme.success : Theme.warning) }
                    Text { anchors.verticalCenter: parent.verticalCenter; width: 260; elide: Text.ElideRight; text: modelData.readable ? modelData.label : "(unreadable definition)"; color: Theme.text; font { family: Theme.font; pixelSize: 13 } }
                    Text { anchors.verticalCenter: parent.verticalCenter; width: 120; text: modelData.kind || ""; color: Theme.textMuted; font { family: Theme.font; pixelSize: 12 } }
                    Text { anchors.verticalCenter: parent.verticalCenter; width: 200; text: "next: " + (modelData.next_due_at_utc || "—"); color: Theme.textMuted; font { family: Theme.font; pixelSize: 12 } }
                    Text { anchors.verticalCenter: parent.verticalCenter; text: "rev " + modelData.revision + (modelData.archived ? "  ·  archived" : modelData.enabled ? "  ·  enabled" : "  ·  disabled"); color: Theme.textDim; font { family: Theme.font; pixelSize: 11 } }
                }
                MouseArea { anchors.fill: parent; onClicked: page.selected = modelData }
            }
        }
        }
        Text { id: specHeading; text: "CREATE A JOB FROM ITS JSON SPECIFICATION (project_id, kind, label, schedule, steps | condition + on_true)  —  or just ask JARVIS in plain language from the Command Center"; color: Theme.textMuted; width: parent.width; wrapMode: Text.Wrap; font { family: Theme.font; pixelSize: 12; letterSpacing: 1 } }
        Rectangle {
            objectName: "jobSpecification"
            width: parent.width
            height: Math.max(180, Math.min(360, viewport.height - jobActions.height - jobListing.height - specHeading.height - createButton.height - form.spacing * 4 - Theme.pad))
            radius: Theme.radiusSmall
            color: Theme.surfaceRaised; border.width: 1; border.color: Theme.border
            ScrollView { anchors.fill: parent; anchors.margins: 6
                TextArea { id: spec; objectName: "jobSpecInput"; color: Theme.text; font { family: Theme.mono; pixelSize: 12 } wrapMode: TextEdit.Wrap; selectByMouse: true; background: Item {} placeholderText: "{ ... }"; placeholderTextColor: Theme.textDim }
            }
        }
        ActionButton { id: createButton; objectName: "createJobButton"; label: "Create job"; onClicked: { bridge.createJob(spec.text); spec.text = "" } }
        }
    }
}
