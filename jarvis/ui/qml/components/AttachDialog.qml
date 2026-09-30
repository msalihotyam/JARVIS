import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// Gate 6C "Projeye bağla" (Salih decision 2026-09-29): attach the open GLOBAL
// conversation to a project, one-way. Pick an existing active project or name
// a new one; the preview says honestly what changes and what does not; one
// button confirms exactly what was previewed. The store's check decides and a
// blocked preview shows its reason with no button. A project is context,
// never authority: nothing here grants, approves or selects anything.
Popup {
    id: dialog
    objectName: "attachDialog"
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(640, parent ? parent.width - 40 : 640); padding: 18
    modal: true; focus: true
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
    property var choices: []
    property string chosenId: ""
    property bool useNew: false
    readonly property var preview: bridge.attachPreview
    readonly property var target: preview.target || ({})
    readonly property bool shown: !!preview.conversation_id
    function openDialog() {
        choices = bridge.projectChoices(); chosenId = ""; useNew = false; newName.text = ""
        bridge.previewAttach("", ""); open()
    }
    function repreview() { bridge.previewAttach(useNew ? "" : chosenId, useNew ? newName.text : "") }
    function when(v) {
        var d = new Date(v)
        return !v ? "" : isNaN(d.getTime()) ? String(v) : d.toLocaleString(Qt.locale(i18n.qtLocale), "d MMM yyyy HH:mm")
    }

    component Choice: Rectangle {
        property string label: ""
        property bool selected: false
        signal picked()
        width: parent ? parent.width : 0; height: 34; radius: 6
        color: selected ? Theme.alpha(Theme.accent, 0.14) : choiceArea.containsMouse ? Theme.alpha(Theme.text, 0.06) : "transparent"
        border.width: 1; border.color: selected ? Theme.alpha(Theme.accent, 0.6) : Theme.border
        Accessible.role: Accessible.RadioButton; Accessible.name: label; Accessible.checked: selected
        Text { x: 12; width: parent.width - 24; anchors.verticalCenter: parent.verticalCenter; text: parent.label
               textFormat: Text.PlainText; elide: Text.ElideRight; color: parent.selected ? Theme.accent : Theme.text; font: Theme.body }
        MouseArea { id: choiceArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: parent.picked() }
    }
    component Line: Text {
        width: parent ? parent.width : 0; wrapMode: Text.Wrap; textFormat: Text.PlainText; color: Theme.text; font: Theme.pageMeta
    }

    background: Rectangle { radius: 12; color: Theme.glassOverlay; border.width: 1; border.color: Theme.glassOverlayEdge }
    contentItem: Column {
        spacing: 10
        Text { text: qsTr("Attach this conversation to a project"); color: Theme.text; font: Theme.uiValue }
        Text { text: qsTr("PROJECT"); color: Theme.textMuted; font: Theme.label }
        Flickable {
            width: parent.width; height: Math.min(projectColumn.height, 34 * 5 + 16); clip: true
            contentHeight: projectColumn.height; boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar {}
            Column {
                id: projectColumn
                width: parent.width; spacing: 4
                Repeater {
                    model: dialog.choices
                    delegate: Choice { objectName: "attachProject"; label: modelData.name
                                       selected: !dialog.useNew && dialog.chosenId === modelData.id
                                       onPicked: { dialog.useNew = false; dialog.chosenId = modelData.id; dialog.repreview() } }
                }
            }
        }
        Row {
            width: parent.width; spacing: 8
            Text { anchors.verticalCenter: parent.verticalCenter; text: qsTr("or a new project:"); color: Theme.textLabel; font: Theme.pageMeta }
            TextField {
                id: newName
                objectName: "attachNewName"
                width: parent.width - x; height: 34; maximumLength: 120; selectByMouse: true
                color: Theme.text; font: Theme.body; placeholderText: qsTr("New project name"); placeholderTextColor: Theme.textDim
                background: Rectangle { radius: 6; color: Theme.surfaceDeep; border.width: 1
                                        border.color: dialog.useNew ? Theme.alpha(Theme.accent, 0.6) : Theme.border }
                onTextEdited: { dialog.useNew = text !== ""; if (!dialog.useNew) dialog.chosenId = ""; dialog.repreview() }
            }
        }
        Line { objectName: "attachNotice"; visible: text !== ""; text: bridge.attachNotice; color: Theme.warning }

        // -- the preview: identity, then (unless blocked) what changes, what does not, and that it is final --
        Column {
            objectName: "attachPreviewPanel"
            visible: dialog.shown
            width: parent.width; spacing: 8
            Line { objectName: "attachIdentity"; font: Theme.pageRowStrong
                   text: qsTr("\"%1\" · %2 turns · created %3").arg(dialog.preview.title || qsTr("New conversation"))
                         .arg(dialog.preview.turns || 0).arg(dialog.when(dialog.preview.created_at)) }
            Line { objectName: "attachTarget"
                   text: dialog.target.new ? qsTr("Target: new project: %1").arg(dialog.target.name || "")
                                           : qsTr("Target: %1").arg(dialog.target.name || "") }
            Line { objectName: "attachBlocked"; visible: !!dialog.preview.blocked; color: Theme.warning
                   text: qsTr("Cannot be attached: %1").arg(dialog.preview.blocked || "") }
            Column {
                visible: !dialog.preview.blocked
                width: parent.width; spacing: 6
                Text { text: qsTr("WHAT CHANGES"); color: Theme.textMuted; font: Theme.label }
                Line { objectName: "attachChanges"
                       text: [qsTr("• The owner becomes %1, permanently.").arg(dialog.target.name || ""),
                              qsTr("• It leaves History and appears in %1's list.").arg(dialog.target.name || ""),
                              qsTr("• From the next message, %1's instruction, file excerpts and memory may be used.").arg(dialog.target.name || ""),
                              qsTr("• The model will see this conversation's earlier messages together with %1's context.").arg(dialog.target.name || "")].join("\n") }
                Text { text: qsTr("WHAT DOES NOT CHANGE"); color: Theme.textMuted; font: Theme.label }
                Line { objectName: "attachUnchanged"
                       text: [qsTr("• Earlier messages and answers are kept as they were."),
                              qsTr("• Past runs stay Global (results, approvals, audit)."),
                              qsTr("• Nothing already sent to a provider changes."),
                              qsTr("• No memory moves."),
                              qsTr("• Permissions, grants and approvals are unaffected: a project is context, not authority.")].join("\n") }
                Line { objectName: "attachIrreversible"; color: Theme.warning
                       text: qsTr("This cannot be undone: the conversation can never return to Global or move to another project.") }
            }
        }
        Row {
            anchors.right: parent.right
            spacing: 8
            ActionButton { objectName: "attachCancel"; label: qsTr("Cancel"); height: 36; onClicked: dialog.close() }
            ActionButton { objectName: "attachConfirm"; label: qsTr("Attach to project"); primary: true; height: 36
                           visible: dialog.shown && !dialog.preview.blocked; enabled: !bridge.busy
                           onClicked: { if (bridge.confirmAttach(dialog.useNew ? "" : dialog.chosenId, dialog.useNew ? newName.text : "")) dialog.close() } }
        }
    }
}
