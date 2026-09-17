import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// The JARVIS edit menu for a text editor: Undo, Cut, Copy, Paste, Select All,
// each enabled only when it can really do something.
//
// Paste is NOT decided here. A plain editor pastes text. The main composer sets
// `attachAware`, and Paste then asks its owner (`pasteRequested`), which routes
// it through the one attachment-aware decision Ctrl+V uses - so the keyboard
// and the menu can never behave differently.
Menu {
    id: menu
    property Item editor: null
    property bool attachAware: false
    signal pasteRequested()

    readonly property bool writable: editor !== null && !editor.readOnly && editor.enabled
    // Read when the menu opens: the clipboard can change while it is closed.
    property bool attachable: false
    onAboutToShow: attachable = attachAware && bridge.clipboardAttachable()

    padding: 5
    implicitWidth: 190
    background: Rectangle {
        implicitWidth: 190
        radius: 8
        color: Theme.alpha("#061424", 0.9)
        border.width: 1
        border.color: Theme.glassEdge
        // A low, quiet drop so the menu separates from what is behind it.
        Rectangle { z: -1; anchors.fill: parent; anchors.topMargin: 3; anchors.leftMargin: 1; anchors.rightMargin: -1
                    anchors.bottomMargin: -3; radius: parent.radius + 1; color: "#000000"; opacity: 0.28 }
    }

    component EditItem: MenuItem {
        id: entry
        property string shortcut: ""
        implicitHeight: 30
        leftPadding: 12; rightPadding: 12
        contentItem: Item {
            implicitHeight: 20
            Text { anchors.verticalCenter: parent.verticalCenter; text: entry.text
                   color: entry.enabled ? Theme.text : Theme.textDim
                   font: Qt.font({ family: Theme.font, pixelSize: 13 }) }
            Text { anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter; text: entry.shortcut
                   color: Theme.textDim; font: Qt.font({ family: Theme.font, pixelSize: 11 }) }
        }
        background: Rectangle {
            radius: 5
            color: entry.highlighted && entry.enabled ? Theme.alpha("#36aaff", 0.16) : "transparent"
            border.width: entry.highlighted && entry.enabled ? 1 : 0
            border.color: Theme.alpha("#49bdfa", 0.35)
        }
    }

    EditItem { objectName: "editUndo"; text: qsTr("Undo"); shortcut: "Ctrl+Z"
               enabled: menu.writable && menu.editor.canUndo; onTriggered: menu.editor.undo() }
    EditItem { objectName: "editCut"; text: qsTr("Cut"); shortcut: "Ctrl+X"
               enabled: menu.writable && menu.editor.selectedText.length > 0; onTriggered: menu.editor.cut() }
    EditItem { objectName: "editCopy"; text: qsTr("Copy"); shortcut: "Ctrl+C"
               enabled: menu.editor !== null && menu.editor.selectedText.length > 0; onTriggered: menu.editor.copy() }
    EditItem { objectName: "editPaste"; text: qsTr("Paste"); shortcut: "Ctrl+V"
               enabled: menu.writable && (menu.editor.canPaste || menu.attachable)
               onTriggered: menu.attachAware ? menu.pasteRequested() : menu.editor.paste() }
    MenuSeparator {
        contentItem: Rectangle { implicitHeight: 1; color: Theme.glassSeparator }
    }
    EditItem { objectName: "editSelectAll"; text: qsTr("Select All"); shortcut: "Ctrl+A"
               enabled: menu.editor !== null && menu.editor.length > 0; onTriggered: menu.editor.selectAll() }
}
