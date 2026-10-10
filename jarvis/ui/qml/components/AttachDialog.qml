import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// Gate 6C "Projeye bağla" (Salih decision 2026-09-29): attach the open GLOBAL
// conversation to a project, one-way. Pick an existing active project or name
// a new one; the preview says honestly what changes and what does not; one
// button confirms exactly what was previewed. The store's check decides and a
// blocked preview shows its reason with no button. A project is context,
// never authority: nothing here grants, approves or selects anything.
// Gate 6E: on the shared dialog shell (accent tone). Focus starts on the first
// project; Enter in the new-name field only previews, it never confirms.
JarvisDialog {
    id: dialog
    objectName: "attachDialog"
    preferredWidth: 640
    label: qsTr("Attach to project")
    title: qsTr("Attach this conversation to a project")
    initialFocus: projects.count > 0 ? projects.itemAt(0) : newName
    confirmItem: confirmButton
    confirmEnabled: !bridge.openRunning               // R1: it attaches the open conversation
    enterGuard: [newName]
    property var choices: []
    property string chosenId: ""
    property bool useNew: false
    readonly property var preview: bridge.attachPreview
    readonly property var target: preview.target || ({})
    readonly property bool shown: !!preview.conversation_id
    // A new project is named once (the Target line); the lines below refer to it.
    readonly property bool named: !target.new
    function openDialog() {
        choices = bridge.projectChoices(); chosenId = ""; useNew = false; newName.text = ""
        bridge.previewAttach("", ""); open()
    }
    function repreview() { bridge.previewAttach(useNew ? "" : chosenId, useNew ? newName.text : "") }
    function when(v) { return !v ? "" : i18n.when(v, Date.now(), "time") || String(v) }

    component Choice: Rectangle {
        id: choice
        property string label: ""
        property bool selected: false
        signal picked()
        width: parent ? parent.width : 0; height: 34; radius: 6
        color: selected ? Theme.alpha(Theme.accent, 0.14) : choiceArea.containsMouse ? Theme.alpha(Theme.text, 0.06) : "transparent"
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
        border.width: 1; border.color: selected ? Theme.alpha(Theme.accent, 0.6) : Theme.border
        activeFocusOnTab: true
        Accessible.role: Accessible.RadioButton; Accessible.name: label; Accessible.checked: selected
        Keys.onReturnPressed: choice.picked()
        Keys.onSpacePressed: choice.picked()
        // The focus ring sits on the row's own edge: the list clips its outside.
        Rectangle { objectName: "attachProjectFocusRing"; visible: choice.activeFocus; anchors.fill: parent; radius: parent.radius
                    color: "transparent"; border.width: 2; border.color: Theme.alpha(Theme.materialGlint, 0.95) }
        Text { id: choiceText; x: 12; width: parent.width - 24; anchors.verticalCenter: parent.verticalCenter; text: parent.label
               textFormat: Text.PlainText; elide: Text.ElideRight; color: parent.selected ? Theme.accent : Theme.text; font: Theme.body }
        MouseArea { id: choiceArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: parent.picked() }
        HoverTip { text: choice.label; active: choiceText.truncated }
    }
    component Line: Text {
        width: parent ? parent.width : 0; wrapMode: Text.Wrap; textFormat: Text.PlainText; color: Theme.text; font: Theme.pageMeta
    }

    Text { text: qsTr("PROJECT"); color: Theme.textMuted; font: Theme.label }
    Flickable {
        width: parent ? parent.width : 0; height: Math.min(projectColumn.height, 34 * 5 + 16); clip: true
        contentHeight: projectColumn.height; boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar {}
        Column {
            id: projectColumn
            width: parent.width; spacing: 4
            Repeater {
                id: projects
                model: dialog.choices
                delegate: Choice { objectName: "attachProject"; label: modelData.name
                                   selected: !dialog.useNew && dialog.chosenId === modelData.id
                                   onPicked: { dialog.useNew = false; dialog.chosenId = modelData.id; dialog.repreview() } }
            }
        }
    }
    Row {
        width: parent ? parent.width : 0; spacing: 8
        Text { id: orNew; anchors.verticalCenter: parent.verticalCenter; text: qsTr("or a new project:"); color: Theme.textLabel; font: Theme.pageMeta }
        TextField {
            id: newName
            objectName: "attachNewName"
            width: parent.width - orNew.width - 8; height: 34; maximumLength: 120; selectByMouse: true
            color: Theme.text; font: Theme.body; placeholderText: qsTr("New project name"); placeholderTextColor: Theme.textDim
            background: Rectangle { radius: 6; color: Theme.surfaceDeep; border.width: 1
                                    border.color: newName.activeFocus ? Theme.commandEdge
                                                  : dialog.useNew ? Theme.alpha(Theme.accent, 0.6) : Theme.border }
            onTextEdited: { dialog.useNew = text !== ""; if (!dialog.useNew) dialog.chosenId = ""; dialog.repreview() }
        }
    }
    Line { objectName: "attachNotice"; visible: text !== ""; text: bridge.attachNotice; color: Theme.warning }
    Line { objectName: "attachPlaceholder"; visible: !dialog.shown; color: Theme.textMuted; text: qsTr("Choose a project to preview.") }

    // -- the preview: identity, then (unless blocked) what changes, what does not, and that it is final --
    Column {
        objectName: "attachPreviewPanel"
        visible: dialog.shown
        width: parent ? parent.width : 0; spacing: 8
        Rectangle {
            objectName: "attachIdentityCard"
            width: parent.width; height: identityColumn.height + 20; radius: Theme.radiusSmall
            color: Theme.glassNested; border.width: 1; border.color: Theme.alpha(Theme.text, 0.10)
            Column {
                id: identityColumn
                x: 12; y: 10; width: parent.width - 24; spacing: 4
                Line { objectName: "attachIdentity"; font: Theme.pageRowStrong
                       text: qsTr("\"%1\" · %2 · created %3").arg(dialog.preview.title || qsTr("New Chat"))
                             .arg(dialog.preview.turns === 1 ? qsTr("1 turn") : qsTr("%1 turns").arg(dialog.preview.turns || 0))
                             .arg(dialog.when(dialog.preview.created_at)) }
                Line { objectName: "attachTarget"
                       text: dialog.target.new ? qsTr("Target: new project: %1").arg(dialog.target.name || "")
                                               : qsTr("Target: %1").arg(dialog.target.name || "") }
            }
        }
        Line { objectName: "attachBlocked"; visible: !!dialog.preview.blocked; color: Theme.warning
               text: qsTr("Cannot be attached: %1").arg(dialog.preview.blocked || "") }
        Column {
            visible: !dialog.preview.blocked
            width: parent.width; spacing: 6
            Text { text: qsTr("WHAT CHANGES"); color: Theme.textMuted; font: Theme.label }
            Line { objectName: "attachChanges"
                   text: (dialog.named
                          ? [qsTr("• The owner becomes %1, permanently.").arg(dialog.target.name || ""),
                             qsTr("• It leaves History and appears in %1's list.").arg(dialog.target.name || ""),
                             qsTr("• From the next message, %1's instruction, file excerpts and memory may be used.").arg(dialog.target.name || ""),
                             qsTr("• The model will see this conversation's earlier messages together with %1's context.").arg(dialog.target.name || "")]
                          : [qsTr("• The owner becomes the new project, permanently."),
                             qsTr("• It leaves History and appears in the new project's list."),
                             qsTr("• From the next message, the new project's instruction, file excerpts and memory may be used."),
                             qsTr("• The model will see this conversation's earlier messages together with the new project's context.")]).join("\n") }
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

    buttons: [
        ActionButton { objectName: "attachCancel"; label: qsTr("Cancel"); height: 36; onClicked: dialog.close() },
        ActionButton { id: confirmButton; objectName: "attachConfirm"; label: qsTr("Attach to project"); primary: true; height: 36
                       visible: dialog.shown && !dialog.preview.blocked
                       onClicked: { if (bridge.confirmAttach(dialog.useNew ? "" : dialog.chosenId, dialog.useNew ? newName.text : "")) dialog.close() } }
    ]
}
