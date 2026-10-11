import QtQuick
import "../theme"

// G6W3 (W1/W3): the project chat's work panel, "IN THIS CHAT". Only stored
// facts, and a section only when it has one: the project instruction in use,
// its active files, the linked folder's NAME (never its path) and the
// capabilities this chat's task results used. No Progress / Outputs (V2.4).
// The page shows it in a project chat only; collapsed it is one button, and
// that choice is a window preference.
Rectangle {
    id: panel
    objectName: "workPanel"
    property real openWidth: 260
    property real maxHeight: 400
    readonly property bool collapsed: bridge.workPanelCollapsed
    readonly property var facts: bridge.workPanel
    readonly property var files: facts.files || []
    readonly property var capabilities: bridge.workPanelCapabilities
    readonly property bool hasAny: !!facts.instruction || files.length > 0 || !!facts.folder || capabilities.length > 0

    width: collapsed ? 40 : openWidth
    height: collapsed ? 40 : Math.min(maxHeight, scroller.y + sections.height + 12)
    Behavior on width { NumberAnimation { duration: Theme.dur(180); easing.type: Easing.OutCubic } }
    radius: Theme.radiusSmall; clip: true
    color: Theme.glassNested; border.width: 1; border.color: Theme.glassNestedEdge

    IconButton {
        objectName: "workPanelToggle"
        x: panel.width - width - 3; y: 3; width: 34; height: 34
        icon: panel.collapsed ? "back" : "forward"
        tip: panel.collapsed ? qsTr("Show what this chat works with") : qsTr("Hide this panel")
        onClicked: bridge.setWorkPanelCollapsed(!panel.collapsed)
    }
    Text {
        objectName: "workPanelTitle"
        visible: !panel.collapsed
        x: 14; y: 14; width: panel.width - 60; elide: Text.ElideRight; renderType: Theme.textRender
        text: qsTr("IN THIS CHAT"); color: Theme.accentSoft; font: Theme.label
    }

    // One section: a drawn icon, its title (and a count on the right), its lines.
    component Section: Column {
        id: section
        property string icon: ""
        property string title: ""
        property string count: ""
        default property alias lines: body.data
        width: parent ? parent.width : 0; spacing: 4
        Item {
            width: section.width; height: 20
            Icon { anchors.verticalCenter: parent.verticalCenter; name: section.icon; size: 14; tint: Theme.textLabel }
            Text { x: 22; anchors.verticalCenter: parent.verticalCenter; width: section.width - 22 - countText.width - 6
                   elide: Text.ElideRight; renderType: Theme.textRender; text: section.title; color: Theme.textLabel; font: Theme.caption }
            Text { id: countText; objectName: "workPanelCount"; anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
                   renderType: Theme.textRender; text: section.count; color: Theme.textMuted; font: Theme.caption }
        }
        Column { id: body; x: 22; width: parent.width - 22; spacing: 2 }
    }
    component Line: Text {
        width: parent ? parent.width : 0; elide: Text.ElideRight; renderType: Theme.textRender; textFormat: Text.PlainText
        color: Theme.text; font: Theme.body
    }

    Flickable {
        id: scroller
        visible: !panel.collapsed
        x: 14; y: 44; width: panel.openWidth - 28; height: panel.height - y - 8
        contentHeight: sections.height; clip: true; boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height + 1
        Column {
            id: sections
            width: parent.width; spacing: 14
            Section {
                objectName: "workPanelInstruction"
                visible: !!panel.facts.instruction
                icon: "pencil"; title: qsTr("Project instruction")
                count: (panel.facts.instruction_chars || 0) + "/" + bridge.projectInstructionCap
                Line { objectName: "workPanelInstructionText"; text: panel.facts.instruction || ""
                       wrapMode: Text.Wrap; maximumLineCount: 3; color: Theme.textLabel }
            }
            Section {
                objectName: "workPanelFiles"
                visible: panel.files.length > 0
                icon: "paperclip"; title: qsTr("Project files"); count: String(panel.files.length)
                Repeater { model: panel.files; Line { objectName: "workPanelFile"; text: modelData } }
            }
            Section {
                objectName: "workPanelFolder"
                visible: !!panel.facts.folder
                icon: "folder"; title: qsTr("Linked folder")
                Line { objectName: "workPanelFolderName"; text: panel.facts.folder || "" }
            }
            Section {
                objectName: "workPanelCapabilities"
                visible: panel.capabilities.length > 0
                icon: "plug"; title: qsTr("Used in this chat")
                // K3 (G6CAPWORD): the capability's word, never its raw id ("Other capability" when unknown);
                // read again on a live language switch (i18n.language).
                Repeater { model: panel.capabilities
                           Line { objectName: "workPanelCapability"; text: { i18n.language; return i18n.word("capability", modelData) } } }
            }
            Text {
                objectName: "workPanelNothing"
                visible: !panel.hasAny
                width: parent.width; wrapMode: Text.Wrap; renderType: Theme.textRender
                text: qsTr("This project has no instruction, active files or linked folder yet, and this chat has used no capability.")
                color: Theme.textMuted; font: Theme.caption
            }
        }
    }
}
