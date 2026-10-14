import QtQuick
import QtQuick.Shapes
import QtQuick.Controls.Basic
import "../theme"

// Gate 6F (L3): the chat header row above the conversation. Left: the scope
// (folder + the owner's name; ARŞİV when archived) and the open
// conversation's title ("Yeni Sohbet" when it has none). Right: "…", the ONE
// conversation menu (ConversationMenu.qml) on the open conversation: the same
// actions, reasons and dialogs. Names elide at the end with their tip. It
// selects, grants and approves nothing.
// Gate 6G F9: H1 "Projeye bağla" left the header - it is a ConversationMenu
// item now (Global only, the same dialog and 6C rules); the header hands the
// window's attach dialog to that menu. H2 the project's name is a faint line
// ABOVE the title and a link to that project's page (bridge.showProject).
// In the Command preset (Q2) the chip is a button: it opens the scope menu
// (projects, "No project", the two actions, then the scope's conversations) - the
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
    // G6CAPWORD (V1/V7): a no-project chat has no scope word (the Command preset's chip shows its icon only).
    readonly property string scopeWord: owner.global ? "" : owner.name || ""
    // F9 H2: outside the Command preset the scope is a faint link line above the title.
    readonly property bool link: !scopeMenu
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
    Text { id: nameSeven; visible: false; font: scopeName.font; textFormat: Text.PlainText; text: header.scopeWord.slice(0, 7) + "…" }
    Text { id: titleSeven; visible: false; font: title.font; textFormat: Text.PlainText; text: title.text.slice(0, 7) + "…" }
    readonly property real nameFloor: Math.min(scopeName.implicitWidth, Math.ceil(nameSeven.implicitWidth) + 1)
    readonly property real titleFloor: Math.min(title.implicitWidth, Math.ceil(titleSeven.implicitWidth) + 1)
    readonly property real chipMax: link ? actions.x - 12 : actions.x - 12 - titleFloor - 26
    // F9 H1: the menu's "Projeye bağla" opens the window's one attach dialog.
    // Set once, never a Binding: a Binding's restore on teardown writes into the shared menu.
    Component.onCompleted: if (conversationMenu && attachDialog) conversationMenu.attachDialog = attachDialog

    // -- the scope chip -------------------------------------------------------
    Rectangle {
        id: chip
        objectName: "chatHeaderScope"
        visible: header.scoped
        readonly property string fullName: header.scopeWord + (header.archived ? " · " + qsTr("archived") : "")
        readonly property real pad: header.link ? 0 : 10
        readonly property real iconSize: header.link ? 14 : 18
        readonly property real iconEnd: pad + iconSize + 6
        readonly property bool pillFits: !header.archived || iconEnd + archivedPill.width + 6 + header.nameFloor + 12 <= header.chipMax
        readonly property real lead: iconEnd + (header.archived ? (pillFits ? archivedPill.width + 6 : 12) : 0)
        readonly property real natural: lead + scopeName.implicitWidth + pad
        x: 0
        // H2: the link tops the name + title block; the preset's chip is centred in the row.
        y: header.link ? Math.max(0, Math.round((header.height - height - 2 - title.height) / 2))
                       : Math.round((header.height - height) / 2)
        width: header.link ? Math.min(natural, Math.max(0, header.chipMax))
                           : Math.min(natural, Math.max(Math.round(header.width * 0.32), lead + header.nameFloor + 12))
        height: header.link ? scopeName.implicitHeight : Theme.pillHeight
        radius: header.link ? 0 : height / 2
        color: header.link ? "transparent" : Theme.alpha(header.tint, header.scopeMenu && (chipArea.containsMouse || scopeList.visible) ? 0.16 : 0.08)
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
        border.width: header.link ? 0 : 1; border.color: Theme.alpha(header.tint, 0.4)
        activeFocusOnTab: header.scopeMenu
        Accessible.role: header.scopeMenu ? Accessible.ButtonMenu : Accessible.Link
        Accessible.name: fullName
        Accessible.onPressAction: header.pickScope()
        Keys.onPressed: function (event) {
            if (header.scopeMenu && (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space || event.key === Qt.Key_Down)) {
                event.accepted = true; header.toggleScope()
            }
        }
        SummaryIcon { x: chip.pad; anchors.verticalCenter: parent.verticalCenter; icon: header.owner.global ? "global" : "project"
                      size: chip.iconSize; tint: header.link ? scopeName.color : header.tint }
        Pill { id: archivedPill; objectName: "chatHeaderScopeArchived"; visible: header.archived && chip.pillFits; dot: false; implicitHeight: 18
               x: chip.iconEnd; anchors.verticalCenter: parent.verticalCenter; text: qsTr("ARCHIVED"); tint: Theme.warning }
        Rectangle { visible: header.archived && !chip.pillFits; x: chip.iconEnd; anchors.verticalCenter: parent.verticalCenter
                    width: 6; height: 6; radius: 3; color: Theme.warning }
        Text {
            id: scopeName
            objectName: "chatHeaderScopeName"
            x: chip.lead; anchors.verticalCenter: parent.verticalCenter
            width: Math.max(0, Math.min(implicitWidth, chip.width - x - chip.pad))
            text: header.scopeWord; textFormat: Text.PlainText; elide: Text.ElideRight
            // H2: the link reads secondary (faint) and lights on hover; the preset's chip as before.
            color: header.link ? (chipArea.containsMouse ? Theme.accent : Theme.textMuted)
                               : header.archived ? Theme.textLabel : Theme.text
            font: header.link ? Theme.caption : Theme.captionStrong
        }
        Rectangle { visible: chip.activeFocus; anchors.fill: parent; anchors.margins: -3; radius: chip.radius + 3
                    color: "transparent"; border.width: 2; border.color: Theme.alpha(Theme.materialGlint, 0.95) }
        MouseArea { id: chipArea; objectName: "chatHeaderScopeLink"; anchors.fill: parent; enabled: header.scoped; hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor; onClicked: header.pickScope() }
        // The whole name when it is cut (or archived); else, on the link, where it leads.
        HoverTip { objectName: "chatHeaderScopeTip"; active: header.link || scopeName.truncated || header.archived
                   text: header.link && !scopeName.truncated && !header.archived ? qsTr("Open the project page") : chip.fullName }
    }
    // The separator: a drawn chevron (no font glyph).
    Shape {
        id: separator
        visible: header.scoped && !header.link
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
        x: separator.visible ? separator.x + separator.width + 10 : 0
        y: header.link && header.scoped ? chip.y + chip.height + 2 : Math.round((header.height - height) / 2)
        width: Math.max(0, Math.min(implicitWidth, actions.x - 12 - x))
        text: bridge.conversationTitle || qsTr("New Chat"); textFormat: Text.PlainText; elide: Text.ElideRight
        color: bridge.conversationTitle ? Theme.text : Theme.textLabel; font: Theme.subtitle
        HoverTip { objectName: "chatHeaderTitleTip"; text: title.text; active: title.truncated }
        // DFMOTION: on a conversation switch the new title fades in with the feed (160 ms).
        readonly property string shownConversation: bridge.conversationId
        onShownConversationChanged: titleIn.restart()
        NumberAnimation { id: titleIn; target: title; property: "opacity"; from: 0; to: 1; duration: Theme.dur(160); easing.type: Theme.ease }
    }

    // -- the actions ------------------------------------------------------------
    Row {
        id: actions
        anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
        spacing: 8
        // V2.1a M4 (I6/I7): the open conversation's "all projects' memory" switch, off by default. It shows
        // the app's confirmed answer (bridge.globalMemoryEnabled); a click only asks, and a refusal leaves it
        // where it was. On, a warning says other projects' memory is read in, read-only, and that the
        // conversation can no longer be moved into a project; the lines take only the room the title can
        // spare (it keeps up to 35 %, never under its floor; the icon stays, the tip has them whole). Not in the Command preset
        // (frozen) nor on a read-only (archived) conversation, which takes no turn to use memory in.
        Item {
            id: memoryWarning
            objectName: "chatHeaderGlobalMemoryWarning"
            visible: memorySwitch.visible && memorySwitch.on
            anchors.verticalCenter: parent.verticalCenter
            width: warningRow.implicitWidth; height: warningRow.implicitHeight
            readonly property real room: Math.max(0, header.width - 12 - 22 - memorySwitch.width - menuButton.width - 3 * actions.spacing
                - (temporarySwitch.visible ? temporarySwitch.width + actions.spacing : 0) - (memoryOffBadge.visible ? memoryOffBadge.width + actions.spacing : 0)
                - Math.min(title.implicitWidth, Math.max(header.titleFloor, Math.round(header.width * 0.35))))
            Row {
                id: warningRow
                spacing: 6
                Icon { anchors.verticalCenter: parent.verticalCenter; name: "warning"; size: 16; tint: Theme.warning }
                Column {
                    anchors.verticalCenter: parent.verticalCenter
                    Text { id: warningLine; objectName: "chatHeaderGlobalMemoryWarningText"; anchors.right: parent.right
                           width: Math.min(implicitWidth, memoryWarning.room); elide: Text.ElideRight; textFormat: Text.PlainText
                           text: qsTr("Other projects' memory is in use, read-only"); color: Theme.warning; font: Theme.caption }
                    Text { id: consequenceLine; objectName: "chatHeaderGlobalMemoryConsequence"; anchors.right: parent.right
                           width: Math.min(implicitWidth, memoryWarning.room); elide: Text.ElideRight; textFormat: Text.PlainText
                           text: qsTr("This conversation can no longer be moved into a project"); color: Theme.textMuted; font: Theme.caption }
                }
            }
            HoverTip { active: warningLine.truncated || consequenceLine.truncated; text: warningLine.text + "\n" + consequenceLine.text }
        }
        Rectangle {
            id: memorySwitch
            objectName: "chatHeaderGlobalMemory"
            readonly property bool on: bridge.globalMemoryEnabled
            visible: bridge.conversationId !== "" && !header.scopeMenu && !bridge.conversationReadOnly
            anchors.verticalCenter: parent.verticalCenter
            width: switchRow.implicitWidth + 20; height: 34; radius: Theme.radiusSmall
            color: memoryArea.containsMouse ? Theme.alpha(Theme.text, 0.07) : "transparent"
            Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
            border.width: 1
            border.color: on ? Theme.alpha(Theme.warning, 0.45) : memoryArea.containsMouse ? Theme.alpha(Theme.accent, 0.45) : "transparent"
            activeFocusOnTab: true
            Accessible.role: Accessible.CheckBox; Accessible.name: qsTr("All projects' memory"); Accessible.checked: on
            Accessible.description: memoryTip.text
            Accessible.onPressAction: header.toggleGlobalMemory()
            Accessible.onToggleAction: header.toggleGlobalMemory()
            Keys.onPressed: function (event) {
                if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
                    event.accepted = true; header.toggleGlobalMemory()
                }
            }
            Row {
                id: switchRow
                anchors.centerIn: parent; spacing: 8
                Icon { anchors.verticalCenter: parent.verticalCenter; name: "memory"; size: 16
                       tint: memorySwitch.on ? Theme.warning : Theme.textLabel }
                Rectangle {
                    id: track
                    objectName: "chatHeaderGlobalMemoryTrack"
                    anchors.verticalCenter: parent.verticalCenter
                    width: 28; height: 16; radius: 8
                    color: memorySwitch.on ? Theme.alpha(Theme.warning, 0.3) : Theme.alpha(Theme.text, 0.1)
                    border.width: 1; border.color: memorySwitch.on ? Theme.warning : Theme.alpha(Theme.text, 0.25)
                    Rectangle { y: 3; width: 10; height: 10; radius: 5; x: memorySwitch.on ? track.width - width - 3 : 3
                                color: memorySwitch.on ? Theme.warning : Theme.textLabel
                                Behavior on x { NumberAnimation { duration: Theme.dur(120); easing.type: Theme.ease } } }
                }
            }
            Rectangle { visible: memorySwitch.activeFocus; anchors.fill: parent; anchors.margins: -3; radius: memorySwitch.radius + 3
                        color: "transparent"; border.width: 2; border.color: Theme.alpha(Theme.materialGlint, 0.95) }
            MouseArea { id: memoryArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                        onClicked: header.toggleGlobalMemory() }
            // Told before the click (K3): turning it on is one-way for attaching (I7).
            HoverTip { id: memoryTip; objectName: "chatHeaderGlobalMemoryTip"
                       text: qsTr("All projects' memory") + "\n" + (memorySwitch.on ? qsTr("Turn off to use only this conversation's own memory. It still cannot be moved into a project.")
                                             : qsTr("Turn on to also read other projects' memory, read-only. The conversation can then never be moved into a project.")) }
        }
        // SEC2UIQML (I21): the open conversation's temporary (memory-off) switch, the same pattern as the
        // one above: it shows the bridge's confirmed answer and a click only asks. Hidden and inert until
        // the bridge has conversationMemoryOff + setConversationMemoryOff (SEC2BRIDGE); while on, a badge.
        Pill {
            id: memoryOffBadge
            objectName: "chatHeaderMemoryOffBadge"
            visible: temporarySwitch.visible && temporarySwitch.on
            anchors.verticalCenter: parent.verticalCenter
            dot: false; implicitHeight: 18; text: qsTr("No memory"); tint: Theme.textLabel
        }
        Rectangle {
            id: temporarySwitch
            objectName: "chatHeaderTemporaryChat"
            readonly property bool available: typeof bridge.setConversationMemoryOff === "function" && typeof bridge.conversationMemoryOff === "boolean"
            readonly property bool on: available && bridge.conversationMemoryOff === true
            visible: available && bridge.conversationId !== "" && !header.scopeMenu && !bridge.conversationReadOnly
            anchors.verticalCenter: parent.verticalCenter
            width: temporaryRow.implicitWidth + 20; height: 34; radius: Theme.radiusSmall
            color: temporaryArea.containsMouse ? Theme.alpha(Theme.text, 0.07) : "transparent"
            Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
            border.width: 1
            border.color: on ? Theme.alpha(Theme.accent, 0.45) : temporaryArea.containsMouse ? Theme.alpha(Theme.accent, 0.45) : "transparent"
            activeFocusOnTab: visible
            Accessible.role: Accessible.CheckBox; Accessible.name: qsTr("Temporary chat (no memory)"); Accessible.checked: on
            Accessible.onPressAction: header.toggleMemoryOff()
            Accessible.onToggleAction: header.toggleMemoryOff()
            Keys.onPressed: function (event) {
                if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
                    event.accepted = true; header.toggleMemoryOff()
                }
            }
            Row {
                id: temporaryRow
                anchors.centerIn: parent; spacing: 8
                Icon { anchors.verticalCenter: parent.verticalCenter; name: "memory"; size: 16; opacity: temporarySwitch.on ? 0.5 : 1
                       tint: temporarySwitch.on ? Theme.accent : Theme.textLabel }
                Rectangle {
                    id: temporaryTrack
                    anchors.verticalCenter: parent.verticalCenter
                    width: 28; height: 16; radius: 8
                    color: temporarySwitch.on ? Theme.alpha(Theme.accent, 0.3) : Theme.alpha(Theme.text, 0.1)
                    border.width: 1; border.color: temporarySwitch.on ? Theme.accent : Theme.alpha(Theme.text, 0.25)
                    Rectangle { y: 3; width: 10; height: 10; radius: 5; x: temporarySwitch.on ? temporaryTrack.width - width - 3 : 3
                                color: temporarySwitch.on ? Theme.accent : Theme.textLabel
                                Behavior on x { NumberAnimation { duration: Theme.dur(120); easing.type: Theme.ease } } }
                }
            }
            Rectangle { visible: temporarySwitch.activeFocus; anchors.fill: parent; anchors.margins: -3; radius: temporarySwitch.radius + 3
                        color: "transparent"; border.width: 2; border.color: Theme.alpha(Theme.materialGlint, 0.95) }
            MouseArea { id: temporaryArea; anchors.fill: parent; enabled: temporarySwitch.available; hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor; onClicked: header.toggleMemoryOff() }
            HoverTip { objectName: "chatHeaderTemporaryChatTip"; text: qsTr("Temporary chat (no memory)") }
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

    function toggleGlobalMemory() { bridge.setGlobalMemoryEnabled(!bridge.globalMemoryEnabled) }
    function toggleMemoryOff() { if (temporarySwitch.available) bridge.setConversationMemoryOff(!temporarySwitch.on) }

    // H2: the link opens its project's page ("Genel" has no line here); the preset's chip its menu.
    function pickScope() {
        if (scopeMenu) toggleScope()
        else if (!owner.global && owner.id) bridge.showProject(owner.id)
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
                                           label: qsTr("No project"); current: !!header.owner.global
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
