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
// Gate 6G Sidebar v2 V7: a no-project conversation has no scope line - the
// title stands alone (in the Command preset the chip stays: it is the menu).
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
    readonly property bool scoped: !owner.global || scopeMenu
    height: big ? 52 : 44

    Rectangle { anchors { left: parent.left; right: parent.right; bottom: parent.bottom } height: 1; color: Theme.alpha(Theme.glassEdge, 0.25) }

    // The names' floor (G2: never under six painted characters), MEASURED as
    // each name is laid out - a hidden Text in its own font and renderer (a
    // TextMetrics gives design advances, narrower than the native rasterizer
    // paints at caption size): seven characters and the ellipsis - one measured
    // character of headroom, so a slightly wider face or rasterizer still
    // paints six (and 1 px past it: an elide at exactly a string's advance
    // keeps one character fewer). The chip's 32 % gives way to its floor first (the title has
    // the room); only if the title would then go under its own floor does the
    // ARCHIVED pill step down to a warning dot.
    Text { id: nameSeven; visible: false; font: scopeName.font; textFormat: Text.PlainText; text: (header.owner.name || "").slice(0, 7) + "…" }
    Text { id: titleSeven; visible: false; font: title.font; textFormat: Text.PlainText; text: title.text.slice(0, 7) + "…" }
    readonly property real nameFloor: Math.min(scopeName.implicitWidth, Math.ceil(nameSeven.implicitWidth) + 1)
    readonly property real titleFloor: Math.min(title.implicitWidth, Math.ceil(titleSeven.implicitWidth) + 1)
    readonly property real chipMax: actions.x - 12 - titleFloor - 26

    // -- the scope chip -------------------------------------------------------
    Rectangle {
        id: chip
        objectName: "chatHeaderScope"
        visible: header.scoped
        readonly property string fullName: (header.owner.name || "") + (header.archived ? " · " + qsTr("archived") : "")
        readonly property bool pillFits: !header.archived || 34 + archivedPill.width + 6 + header.nameFloor + 12 <= header.chipMax
        readonly property real lead: 10 + 18 + 6 + (header.archived ? (pillFits ? archivedPill.width + 6 : 12) : 0)
        readonly property real natural: lead + scopeName.implicitWidth + 12
        x: 0; anchors.verticalCenter: parent.verticalCenter
        width: Math.min(natural, Math.max(Math.round(header.width * 0.32), lead + header.nameFloor + 12))
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
        Pill { id: archivedPill; objectName: "chatHeaderScopeArchived"; visible: header.archived && chip.pillFits; dot: false; implicitHeight: 18
               x: 34; anchors.verticalCenter: parent.verticalCenter; text: qsTr("ARCHIVED"); tint: Theme.warning }
        Rectangle { visible: header.archived && !chip.pillFits; x: 34; anchors.verticalCenter: parent.verticalCenter
                    width: 6; height: 6; radius: 3; color: Theme.warning }
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
        visible: header.scoped
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
        x: header.scoped ? separator.x + separator.width + 10 : 0; anchors.verticalCenter: parent.verticalCenter
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
            // M1 (K4): open only when THIS "…" opened it - not when the open conversation's own sidebar row did.
            readonly property bool open: !!header.conversationMenu && header.conversationMenu.open && header.conversationMenu.opener === menuButton
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
                                 title, Qt.point(0, title.height + 10), undefined, undefined, menuButton)
    }

    // -- the Command preset's scope menu (Q2) -------------------------------------
    function toggleScope() { if (scopeList.visible) scopeList.close(); else scopeList.popupAt(chip, Qt.point(0, chip.height + 8), { trigger: chip }) }
    MenuSurface {
        id: scopeList
        objectName: "chatHeaderScopeMenu"
        property var choices: []
        width: 320
        onAboutToShow: choices = bridge.projectChoices()
        Column {
            width: scopeList.availableWidth
            spacing: 2
            Text { text: qsTr("PROJECTS"); color: Theme.textMuted; font: Theme.label; leftPadding: 12; bottomPadding: 4 }
            Repeater {
                model: scopeList.choices
                delegate: ConversationMenu.ScopeChoice { objectName: "scopeMenuProject"; width: scopeList.availableWidth; label: modelData.name
                                                         current: modelData.id === header.owner.id
                                                         onTriggered: { scopeList.close(); bridge.openProjectScope(modelData.id) } }
            }
            ConversationMenu.Divider { width: scopeList.availableWidth }
            ConversationMenu.ScopeChoice { objectName: "scopeMenuGlobal"; width: scopeList.availableWidth
                                           label: qsTr("Global"); current: !!header.owner.global
                                           onTriggered: { scopeList.close(); bridge.openProjectScope("") } }
            ConversationMenu.Divider { width: scopeList.availableWidth }
            ConversationMenu.ScopeChoice { objectName: "scopeMenuAll"; width: scopeList.availableWidth; action: true
                                           label: qsTr("All projects"); onTriggered: { scopeList.close(); bridge.showProject("") } }
            ConversationMenu.Divider { width: scopeList.availableWidth }
            Text { text: qsTr("CONVERSATIONS"); color: Theme.textMuted; font: Theme.label; leftPadding: 12; bottomPadding: 4 }
            ListView {
                objectName: "scopeMenuConversations"
                width: scopeList.availableWidth; height: Math.min(contentHeight, 6 * (Theme.menuRowHeight + 2) - 2)
                clip: true; spacing: 2
                keyNavigationEnabled: false             // Up / Down are the menu's (MenuSurface)
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar { }
                model: bridge.scopeConversations
                delegate: ConversationMenu.ScopeChoice { objectName: "scopeMenuConversation"; width: ListView.view.width
                                                         label: modelData.title || qsTr("New Chat"); muted: !modelData.title; current: modelData.active
                                                         onTriggered: { scopeList.close(); bridge.openConversation(modelData.id) } }
            }
        }
    }
}
