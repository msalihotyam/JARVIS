import QtQuick
import QtQuick.Shapes
import QtQuick.Controls.Basic
import "../theme"

// Gate 6F (L3): the chat header row above the conversation. Left: the scope
// chip (globe or folder + the owner's name; ARŞİV when archived) > the open
// conversation's title ("Yeni Sohbet" when it has none). Right: "Projeye
// bağla" - only for a writable Global conversation, the same attach dialog
// and rules (6C) - and "…", the ONE conversation menu (ConversationMenu.qml)
// on the open conversation: the same actions, reasons and dialogs. Names
// elide at the end with their tip. It selects, grants and approves nothing.
// In the Command preset (Q2) the chip is a button: it opens the scope menu
// (projects, Genel, the two actions, then the scope's conversations) - the
// TopBar's former selector and switcher menus, moved.
Item {
    id: header
    objectName: "chatHeader"
    property Item conversationMenu: null
    property var attachDialog: null
    property bool scopeMenu: false
    readonly property bool big: (Window.width || 1920) >= 1600
    readonly property var owner: bridge.conversationProject || ({})
    readonly property bool archived: !!owner.archived
    readonly property color tint: archived ? Theme.warning : owner.global ? Theme.textLabel : Theme.accent
    height: big ? 52 : 44

    Rectangle { anchors { left: parent.left; right: parent.right; bottom: parent.bottom } height: 1; color: Theme.alpha(Theme.glassEdge, 0.25) }

    // -- the scope chip -------------------------------------------------------
    Rectangle {
        id: chip
        objectName: "chatHeaderScope"
        readonly property string fullName: (header.owner.name || "") + (header.archived ? " · " + qsTr("archived") : "")
        readonly property real lead: 10 + 18 + 6 + (header.archived ? archivedPill.width + 6 : 0)
        readonly property real natural: lead + scopeName.implicitWidth + 12
        x: 0; anchors.verticalCenter: parent.verticalCenter
        width: Math.min(natural, Math.round(header.width * 0.32))
        height: Theme.pillHeight; radius: height / 2
        color: Theme.alpha(header.tint, header.scopeMenu && (chipArea.containsMouse || scopeList.visible) ? 0.16 : 0.08)
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
        border.width: 1; border.color: Theme.alpha(header.tint, 0.4)
        activeFocusOnTab: header.scopeMenu
        Accessible.role: header.scopeMenu ? Accessible.ButtonMenu : Accessible.StaticText
        Accessible.name: fullName
        Accessible.onPressAction: if (header.scopeMenu) header.toggleScope()
        Keys.onPressed: function (event) {
            if (header.scopeMenu && (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space || event.key === Qt.Key_Down)) {
                event.accepted = true; header.toggleScope()
            }
        }
        SummaryIcon { x: 10; anchors.verticalCenter: parent.verticalCenter; icon: header.owner.global ? "global" : "project"; tint: header.tint }
        Pill { id: archivedPill; objectName: "chatHeaderScopeArchived"; visible: header.archived; dot: false; implicitHeight: 18
               x: 34; anchors.verticalCenter: parent.verticalCenter; text: qsTr("ARCHIVED"); tint: Theme.warning }
        Text {
            id: scopeName
            objectName: "chatHeaderScopeName"
            x: chip.lead; anchors.verticalCenter: parent.verticalCenter
            width: Math.max(0, Math.min(implicitWidth, chip.width - x - 12))
            text: header.owner.name || ""; textFormat: Text.PlainText; elide: Text.ElideRight
            color: header.archived ? Theme.textLabel : Theme.text; font: Theme.captionStrong
        }
        Rectangle { visible: chip.activeFocus; anchors.fill: parent; anchors.margins: -3; radius: chip.radius + 3
                    color: "transparent"; border.width: 2; border.color: Theme.alpha(Theme.materialGlint, 0.95) }
        MouseArea { id: chipArea; anchors.fill: parent; enabled: header.scopeMenu; hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor; onClicked: header.toggleScope() }
        HoverTip { objectName: "chatHeaderScopeTip"; text: chip.fullName; active: scopeName.truncated || header.archived }
    }
    // The separator: a drawn chevron (no font glyph).
    Shape {
        id: separator
        x: chip.width + 10; anchors.verticalCenter: parent.verticalCenter
        width: 6; height: 10
        preferredRendererType: Shape.CurveRenderer
        ShapePath { strokeColor: Theme.textDim; strokeWidth: 1.5; fillColor: "transparent"
                    capStyle: ShapePath.RoundCap; joinStyle: ShapePath.RoundJoin
                    startX: 1; startY: 1; PathLine { x: 5; y: 5 } PathLine { x: 1; y: 9 } }
    }
    Text {
        id: title
        objectName: "chatHeaderTitle"
        x: separator.x + separator.width + 10; anchors.verticalCenter: parent.verticalCenter
        width: Math.max(0, Math.min(implicitWidth, actions.x - 12 - x))
        text: bridge.conversationTitle || qsTr("New Chat"); textFormat: Text.PlainText; elide: Text.ElideRight
        color: bridge.conversationTitle ? Theme.text : Theme.textLabel; font: Theme.subtitle
        HoverTip { objectName: "chatHeaderTitleTip"; text: title.text; active: title.truncated }
    }

    // -- the actions ------------------------------------------------------------
    Row {
        id: actions
        anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
        spacing: 8
        ActionButton {
            id: attachButton
            objectName: "chatHeaderAttach"
            visible: bridge.canAttach
            anchors.verticalCenter: parent.verticalCenter
            height: 32; compact: true
            label: qsTr("Attach to a project…")
            onClicked: if (header.attachDialog) { header.attachDialog.opener = attachButton; header.attachDialog.openDialog() }
        }
        Rectangle {
            id: menuButton
            objectName: "chatHeaderMenu"
            readonly property bool open: !!header.conversationMenu && header.conversationMenu.open && header.conversationMenu.rowId === ""
            visible: bridge.conversationId !== ""
            width: 34; height: 34; radius: Theme.radiusSmall
            color: open ? Theme.alpha(Theme.accent, 0.14) : menuArea.containsMouse ? Theme.alpha(Theme.text, 0.07) : "transparent"
            Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
            border.width: 1; border.color: open || menuArea.containsMouse ? Theme.alpha(Theme.accent, 0.45) : "transparent"
            activeFocusOnTab: true
            Accessible.role: Accessible.ButtonMenu; Accessible.name: qsTr("Conversation actions")
            Accessible.onPressAction: header.openMenu()
            // Escape closes its own open menu (the menu takes no focus) before the
            // window's Escape chain sees it.
            Keys.onShortcutOverride: function (event) { if (event.key === Qt.Key_Escape && menuButton.open) event.accepted = true }
            Keys.onPressed: function (event) {
                var key = event.key
                if (key === Qt.Key_Escape && menuButton.open) {
                    event.accepted = true; header.conversationMenu.dismiss()
                } else if (key === Qt.Key_Return || key === Qt.Key_Enter || key === Qt.Key_Space || key === Qt.Key_Menu
                        || (key === Qt.Key_F10 && (event.modifiers & Qt.ShiftModifier))) {
                    event.accepted = true; header.openMenu()
                }
            }
            Row {
                anchors.centerIn: parent; spacing: 3
                Repeater { model: 3; Rectangle { width: 4; height: 4; radius: 2; color: menuButton.open ? Theme.accent : Theme.textLabel } }
            }
            Rectangle { visible: menuButton.activeFocus; anchors.fill: parent; anchors.margins: -3; radius: menuButton.radius + 3
                        color: "transparent"; border.width: 2; border.color: Theme.alpha(Theme.materialGlint, 0.95) }
            MouseArea { id: menuArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                        onClicked: header.openMenu() }
            HoverTip { text: qsTr("Conversation actions"); active: !menuButton.open }
        }
    }
    // The menu opens under "…", right-aligned; the rename editor under the title.
    function openMenu() {
        if (!conversationMenu) return
        if (menuButton.open) { conversationMenu.dismiss(); return }
        conversationMenu.openFor("", "", menuButton, Qt.point(menuButton.width - 300, menuButton.height + 6),
                                 title, Qt.point(0, title.height + 10))
    }

    // -- the Command preset's scope menu (Q2) -------------------------------------
    function toggleScope() { if (scopeList.visible) scopeList.close(); else scopeList.open() }
    ConversationMenu.ScopeMenu {
        id: scopeList
        objectName: "chatHeaderScopeMenu"
        property var choices: []
        restY: chip.y + chip.height + 8; x: 0
        width: 320
        onAboutToShow: choices = bridge.projectChoices()
        contentItem: Column {
            spacing: 2
            Text { text: qsTr("PROJECTS"); color: Theme.textMuted; font: Theme.label; leftPadding: 12; bottomPadding: 4 }
            Repeater {
                model: scopeList.choices
                delegate: ConversationMenu.ScopeChoice { objectName: "scopeMenuProject"; width: scopeList.availableWidth; label: modelData.name
                                                         current: modelData.id === header.owner.id
                                                         onChosen: { scopeList.close(); bridge.openProjectScope(modelData.id) } }
            }
            ConversationMenu.Divider { width: scopeList.availableWidth }
            ConversationMenu.ScopeChoice { objectName: "scopeMenuGlobal"; width: scopeList.availableWidth
                                           label: qsTr("Global"); current: !!header.owner.global
                                           onChosen: { scopeList.close(); bridge.openProjectScope("") } }
            ConversationMenu.Divider { width: scopeList.availableWidth }
            ConversationMenu.ScopeChoice { objectName: "scopeMenuAll"; width: scopeList.availableWidth; action: true
                                           label: qsTr("All projects"); onChosen: { scopeList.close(); bridge.showProject("") } }
            ConversationMenu.Divider { width: scopeList.availableWidth }
            Text { text: qsTr("CONVERSATIONS"); color: Theme.textMuted; font: Theme.label; leftPadding: 12; bottomPadding: 4 }
            ListView {
                objectName: "scopeMenuConversations"
                width: scopeList.availableWidth; height: Math.min(contentHeight, 6 * (Theme.menuRowHeight + 2) - 2)
                clip: true; spacing: 2
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar { }
                model: bridge.scopeConversations
                delegate: ConversationMenu.ScopeChoice { objectName: "scopeMenuConversation"; width: ListView.view.width
                                                         label: modelData.title || qsTr("New Chat"); muted: !modelData.title; current: modelData.active
                                                         onChosen: { scopeList.close(); bridge.openConversation(modelData.id) } }
            }
        }
    }
}
