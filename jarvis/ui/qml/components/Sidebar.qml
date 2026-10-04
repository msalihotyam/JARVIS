import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// Gate 6F (L2): the one left panel of the home page. Top to bottom: the
// navigation (Genel, projects, the selected scope's conversations - F3), the
// summary tiles (SummaryRail) and the clock strip (LeftContextCard). The
// navigation takes whatever height the tiles and the strip leave.
// Navigation (L4): the same bridge calls as the TopBar scope group - a scope
// pick opens that scope's newest non-archived conversation (or a new one), a
// row pick opens that conversation, "+" starts one in the selected scope; a
// right-click on a row opens the conversation menu for THAT row (Q8). It
// selects, grants and approves nothing.
Rectangle {
    id: sidebar
    objectName: "sidebar"
    property date now: new Date()
    property string activeKey: ""
    property Item conversationMenu: null
    // The 1920-class sizes (the mockup's breakpoint), else the compact ones.
    readonly property bool big: (Window.width || 1920) >= 1600
    readonly property int pad: big ? 14 : 10
    readonly property alias rail: summaryRail
    readonly property alias nav: navigation
    signal picked(string key, real cardY, real cardHeight)
    signal menuRequested(string card, Item anchor, bool canUp, bool canDown)
    signal approvalRequested()
    signal newProjectRequested()

    radius: 12
    color: Theme.glassSecondaryShell
    border.width: 1; border.color: Theme.glassEdge
    // Inner light from the upper edge, as the other secondary glass has.
    Rectangle {
        anchors.fill: parent; anchors.margins: 1; radius: parent.radius - 1
        gradient: Gradient { GradientStop { position: 0; color: Theme.glassMicro } GradientStop { position: 0.35; color: "transparent" } }
    }

    // One navigation row: the 6E ListRow states at the sidebar's size - hover
    // is a tonal lift, open / selected the accent fill with the 2 px edge
    // mark, keyboard focus the ring. The name elides with its tip; the count
    // or time never elides. Return / Space activate; Menu / Shift+F10 (and a
    // right-click) ask for its menu. While a run is busy a pick is not
    // `available`: dimmed, its click does nothing and its tip says why.
    component NavRow: Rectangle {
        id: row
        property string title: ""
        property string titleName: ""
        property string meta: ""
        property string metaName: ""
        property string icon: ""
        property string itemId: ""
        property bool current: false
        property bool muted: false
        readonly property bool available: !bridge.busy
        signal activated()
        signal contextRequested(point position)
        width: parent ? parent.width : 0
        height: sidebar.rowH
        opacity: available ? 1 : 0.38
        radius: Theme.radiusSmall
        color: current ? Theme.alpha(Theme.accent, 0.10) : area.containsMouse ? Theme.alpha(Theme.text, 0.05) : "transparent"
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
        activeFocusOnTab: true
        Accessible.role: Accessible.Button
        Accessible.name: meta !== "" ? title + ", " + meta : title
        Accessible.onPressAction: if (available) activated()
        Accessible.description: available ? "" : busyReason.text
        Keys.onShortcutOverride: function (event) {
            if (event.key === Qt.Key_Escape && sidebar.conversationMenu && sidebar.conversationMenu.open) event.accepted = true
        }
        Keys.onPressed: function (event) {
            if (event.key === Qt.Key_Escape && sidebar.conversationMenu && sidebar.conversationMenu.open) {
                event.accepted = true; sidebar.conversationMenu.dismiss()
            } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
                event.accepted = true; if (row.available) row.activated()
            } else if (event.key === Qt.Key_Menu || (event.key === Qt.Key_F10 && (event.modifiers & Qt.ShiftModifier))) {
                event.accepted = true; row.contextRequested(Qt.point(row.width / 2, row.height / 2))
            }
        }
        // A row the keyboard reaches is scrolled into view in its list.
        onActiveFocusChanged: if (activeFocus) reveal()
        function reveal() {
            for (var p = parent; p; p = p.parent) {
                if (p.contentY === undefined || !p.contentItem) continue
                var top = row.mapToItem(p.contentItem, 0, 0).y
                if (top < p.contentY) p.contentY = top
                else if (top + row.height > p.contentY + p.height) p.contentY = top + row.height - p.height
                return
            }
        }
        Rectangle {
            id: well
            visible: row.icon !== ""
            x: 6; anchors.verticalCenter: parent.verticalCenter
            width: sidebar.well; height: width; radius: Theme.radiusTiny
            color: Theme.alpha(row.current ? Theme.accent : Theme.text, row.current ? 0.16 : 0.06)
            SummaryIcon { anchors.centerIn: parent; icon: row.icon; tint: row.current ? Theme.accent : Theme.textLabel }
        }
        Text {
            id: nameText
            objectName: row.titleName
            x: row.icon !== "" ? well.x + well.width + 8 : 10
            anchors.verticalCenter: parent.verticalCenter
            width: Math.max(0, Math.min(implicitWidth, metaText.x - 8 - x))
            text: row.title; textFormat: Text.PlainText; elide: Text.ElideRight
            color: row.current ? Theme.accent : row.muted ? Theme.textLabel : Theme.text
            font: sidebar.big ? Theme.pageRow : Theme.body
            Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
        }
        Text { id: metaText; objectName: row.metaName; text: row.meta; textFormat: Text.PlainText
               x: row.width - 10 - implicitWidth; anchors.verticalCenter: parent.verticalCenter
               color: Theme.textMuted; font: Theme.captionFigures }
        Rectangle { objectName: "sidebarRowEdge"; x: 0; y: 8; width: 2; height: row.height - 16; radius: 1; color: Theme.accent
                    opacity: row.current ? 1 : 0
                    Behavior on opacity { NumberAnimation { duration: Theme.hoverMs } } }
        Rectangle { objectName: "sidebarRowFocusRing"; visible: row.activeFocus; anchors.fill: parent; anchors.margins: 1; radius: row.radius - 1
                    color: "transparent"; border.width: 2; border.color: Theme.alpha(Theme.materialGlint, 0.95) }
        MouseArea {
            id: area
            anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.LeftButton | Qt.RightButton
            cursorShape: Qt.PointingHandCursor
            onClicked: function (mouse) {
                if (mouse.button === Qt.RightButton) row.contextRequested(Qt.point(mouse.x, mouse.y))
                else if (row.available) row.activated()
            }
        }
        HoverTip { objectName: "sidebarRowTip"; text: row.available ? row.title : busyReason.text; active: nameText.truncated || !row.available }
    }
    // A small text link (PROJELER, archived projects, Arşiv): hover lifts its
    // ink; a tip when it says less than its whole meaning.
    component NavLink: Rectangle {
        id: link
        property string text: ""
        property string count: ""
        property string tip: ""
        property bool dot: false
        property bool current: false
        property font face: Theme.caption
        signal activated()
        width: linkRow.width + 16; height: 26; radius: Theme.radiusTiny
        color: current ? Theme.alpha(Theme.accent, 0.10) : linkArea.containsMouse ? Theme.alpha(Theme.text, 0.05) : "transparent"
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
        activeFocusOnTab: true
        Accessible.role: Accessible.Link
        Accessible.name: (tip || text) + (count !== "" ? ", " + count : "")
        Accessible.onPressAction: activated()
        Keys.onReturnPressed: activated()
        Keys.onSpacePressed: activated()
        Row {
            id: linkRow
            x: 8; anchors.verticalCenter: parent.verticalCenter; spacing: 8
            Rectangle { visible: link.dot; width: 6; height: 6; radius: 3; color: Theme.warning; anchors.verticalCenter: parent.verticalCenter }
            Text { visible: link.text !== ""; text: link.text; font: link.face; anchors.verticalCenter: parent.verticalCenter
                   color: linkArea.containsMouse || link.current ? Theme.text : Theme.textMuted
                   Behavior on color { ColorAnimation { duration: Theme.hoverMs } } }
            Text { visible: link.count !== ""; text: link.count; font: Theme.captionFigures; color: Theme.textDim
                   anchors.verticalCenter: parent.verticalCenter }
        }
        Rectangle { visible: link.current; x: 0; y: 6; width: 2; height: link.height - 12; radius: 1; color: Theme.accent }
        Rectangle { objectName: "sidebarLinkFocusRing"; visible: link.activeFocus; anchors.fill: parent; anchors.margins: 1; radius: link.radius - 1
                    color: "transparent"; border.width: 2; border.color: Theme.alpha(Theme.materialGlint, 0.95) }
        MouseArea { id: linkArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: link.activated() }
        HoverTip { text: link.tip; active: link.tip !== "" }
    }
    // A drawn plus (no font glyph): the PROJELER "+" (ghost) and the round
    // accent "+" of SOHBETLER. Not `allowed`: dimmed, its reason in the tip.
    component PlusButton: Rectangle {
        id: plus
        property string tip: ""
        property bool allowed: true
        property bool accent: false
        signal activated()
        readonly property bool hovered: plusArea.containsMouse
        radius: accent ? width / 2 : Theme.radiusTiny
        color: accent ? Theme.alpha(Theme.accent, hovered && allowed ? 0.28 : 0.18)
                      : hovered && allowed ? Theme.alpha(Theme.text, 0.07) : "transparent"
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
        border.width: accent ? 1 : 0; border.color: Theme.alpha(Theme.commandEdge, 0.6)
        opacity: allowed ? 1 : 0.38
        scale: plusArea.pressed && allowed ? 0.94 : 1
        Behavior on scale { enabled: Theme.ambient; NumberAnimation { duration: 90 } }
        activeFocusOnTab: true
        Accessible.role: Accessible.Button; Accessible.name: tip
        Accessible.onPressAction: if (allowed) activated()
        Keys.onReturnPressed: if (allowed) activated()
        Keys.onSpacePressed: if (allowed) activated()
        Rectangle { anchors.centerIn: parent; width: plus.accent ? 13 : 12; height: 1.8; radius: 0.9; color: plus.accent ? Theme.text : Theme.textLabel }
        Rectangle { anchors.centerIn: parent; width: 1.8; height: plus.accent ? 13 : 12; radius: 0.9; color: plus.accent ? Theme.text : Theme.textLabel }
        Rectangle { objectName: "sidebarPlusFocusRing"; visible: plus.activeFocus; anchors.fill: parent; anchors.margins: -3; radius: plus.radius + 3
                    color: "transparent"; border.width: 2; border.color: Theme.alpha(Theme.materialGlint, 0.95) }
        MouseArea { id: plusArea; anchors.fill: parent; hoverEnabled: true
                    cursorShape: plus.allowed ? Qt.PointingHandCursor : Qt.ArrowCursor; onClicked: if (plus.allowed) plus.activated() }
        HoverTip { objectName: "sidebarPlusTip"; text: plus.tip }
    }

    // -- the navigation's data and metrics ------------------------------------
    Text { id: busyReason; visible: false; text: qsTr("Wait for the current run to finish.") }
    readonly property var owner: bridge.conversationProject || ({})
    readonly property var projects: bridge.projectRecords.filter(function (p) { return p.active })
    readonly property int archivedProjects: bridge.projectRecords.length - projects.length
    readonly property var ownerRecord: bridge.projectRecords.filter(function (p) { return p.id === sidebar.owner.id })[0] || ({})
    readonly property int scopeArchived: owner.global ? bridge.archivedConversations.length : (ownerRecord.archived_count || 0)
    readonly property int rowH: big ? Theme.rowHeight : Theme.menuRowHeight
    readonly property int well: big ? 26 : 22
    readonly property int step: rowH + 2
    readonly property int labelH: 22
    readonly property int linkH: 26
    // Below 760 px of height the archived-projects link folds into the
    // PROJELER heading as "(n)", so the conversations keep their room.
    readonly property bool fold: (Window.height || 1080) < 760
    function count(n) { return n > 0 ? String(n) : "–" }
    // The conversations by the local calendar day of their last change.
    function stamp(v) { return new Date(String(v || "").replace(/(\.\d{3})\d+/, "$1")).getTime() }
    readonly property real today: new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
    readonly property var groups: {
        var start = new Date(today), yesterday = new Date(start.getFullYear(), start.getMonth(), start.getDate() - 1).getTime()
        var out = [[], [], []]
        var list = bridge.scopeConversations
        for (var i = 0; i < list.length; ++i) {
            var t = stamp(list[i].updated_at)
            out[isNaN(t) || t < yesterday ? 2 : t < today ? 1 : 0].push(list[i])
        }
        return [{ key: "today", label: qsTr("Today"), items: out[0] }, { key: "yesterday", label: qsTr("Yesterday"), items: out[1] },
                { key: "earlier", label: qsTr("Earlier"), items: out[2] }].filter(function (g) { return g.items.length > 0 })
    }
    function when(c, group) {
        var t = stamp(c.updated_at)
        return isNaN(t) ? "" : new Date(t).toLocaleString(Qt.locale(i18n.qtLocale), group === "earlier" ? "d MMM" : "HH:mm")
    }
    // Height: the fixed blocks, then the project list (5 / 3 whole rows, fewer
    // while the conversations would keep less than two whole rows under a
    // label), then the conversations - whole rows only, then their own scroll.
    readonly property real fixedH: rowH + 2 + 28 + 2 + (!fold && archivedProjects > 0 ? linkH + 2 : 0) + 9 + 2 + 32 + 2
    readonly property real room: navigation.height - fixedH
    readonly property int projectRows: projects.length === 0 ? 0
        : Math.max(1, Math.min(big ? 5 : 3, projects.length, Math.floor((room - (labelH + 2 * step)) / step)))
    readonly property real conversationRoom: room - projectRows * step
    // The tallest run of whole items, from the top, that fits the room.
    readonly property real conversationH: {
        var sizes = []
        for (var g = 0; g < groups.length; ++g) {
            sizes.push(labelH)
            for (var i = 0; i < groups[g].items.length; ++i) sizes.push(rowH)
        }
        if (groups.length === 0) sizes.push(emptyLine.implicitHeight)
        if (scopeArchived > 0) sizes.push(linkH + 4)
        var h = 0
        for (var k = 0; k < sizes.length; ++k) {
            var next = h + (k > 0 ? 2 : 0) + sizes[k]
            if (next > conversationRoom) break
            h = next
        }
        return h
    }

    // The navigation region: everything above the tiles.
    Item {
        id: navigation
        objectName: "sidebarNav"
        x: sidebar.pad; y: sidebar.pad
        width: sidebar.width - 2 * sidebar.pad
        height: Math.max(0, (summaryRail.height > 0 ? summaryRail.y - 8 : clock.y - 6) - y)

        Column {
            width: parent.width; spacing: 2

            NavRow {
                objectName: "sidebarGlobal"
                titleName: "sidebarGlobalName"; metaName: "sidebarGlobalCount"
                icon: "global"; title: qsTr("Global"); meta: sidebar.count(bridge.conversations.length)
                current: !!sidebar.owner.global
                onActivated: bridge.openProjectScope("")
            }

            // PROJELER: the label is a link to the Projects page (Q10); "+" opens
            // it with its create field focused (Q9).
            Item {
                width: parent.width; height: 28
                NavLink {
                    id: projectsLink
                    objectName: "sidebarProjectsLink"
                    x: 2; anchors.bottom: parent.bottom
                    text: i18n.upper(qsTr("PROJECTS")); face: Theme.label; tip: qsTr("All projects")
                    onActivated: bridge.showProject("")
                }
                NavLink {
                    objectName: "sidebarArchivedProjectsCount"
                    visible: sidebar.fold && sidebar.archivedProjects > 0
                    x: projectsLink.x + projectsLink.width; anchors.bottom: parent.bottom
                    dot: true; count: "(" + sidebar.archivedProjects + ")"; tip: qsTr("Archived projects")
                    current: !!sidebar.owner.archived
                    onActivated: bridge.showProject("")
                }
                PlusButton {
                    objectName: "sidebarNewProject"
                    anchors.right: parent.right; anchors.rightMargin: 4; anchors.bottom: parent.bottom; anchors.bottomMargin: 1
                    width: 24; height: 24
                    tip: qsTr("New project")
                    onActivated: sidebar.newProjectRequested()
                }
            }

            Flickable {
                id: projectList
                objectName: "sidebarProjectList"
                visible: sidebar.projectRows > 0
                width: parent.width; height: sidebar.projectRows * sidebar.step - 2
                clip: true; contentHeight: projectColumn.height
                // The drawn icons stay inside the clip with every scene-graph backend.
                layer.enabled: true
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                // Whole rows at rest.
                onMovementEnded: contentY = Math.min(Math.round(contentY / sidebar.step) * sidebar.step, Math.max(0, contentHeight - height))
                // The selected project's row stays in view (P3): on open, on a scope change, when the
                // list or its room changes. Only those events move it - never the reader's own scrolling.
                function keepSelected() {
                    var at = -1
                    for (var i = 0; i < sidebar.projects.length; ++i) if (sidebar.projects[i].id === sidebar.owner.id) at = i
                    if (at < 0 || height <= 0) return
                    var top = at * sidebar.step, bottom = top + sidebar.step - 2
                    if (top < contentY) contentY = top
                    else if (bottom > contentY + height) contentY = Math.min(Math.max(0, contentHeight - height), Math.max(0, (at + 1 - Math.round((height + 2) / sidebar.step)) * sidebar.step))
                }
                readonly property string selected: sidebar.owner.id || ""
                onSelectedChanged: Qt.callLater(keepSelected)
                onHeightChanged: Qt.callLater(keepSelected)
                onContentHeightChanged: Qt.callLater(keepSelected)
                Component.onCompleted: Qt.callLater(keepSelected)
                Column {
                    id: projectColumn
                    width: projectList.width - (projectList.contentHeight > projectList.height ? 10 : 0); spacing: 2
                    Repeater {
                        model: sidebar.projects
                        NavRow {
                            objectName: "sidebarProject"
                            titleName: "sidebarProjectName"; metaName: "sidebarProjectCount"
                            icon: "project"; itemId: modelData.id; title: modelData.name
                            meta: sidebar.count(modelData.conversation_count || 0)
                            current: sidebar.owner.id === modelData.id
                            onActivated: bridge.openProjectScope(modelData.id)
                        }
                    }
                }
            }

            NavLink {
                objectName: "sidebarArchivedProjects"
                visible: !sidebar.fold && sidebar.archivedProjects > 0
                height: sidebar.linkH
                dot: true; text: qsTr("Archived projects"); count: String(sidebar.archivedProjects)
                current: !!sidebar.owner.archived
                onActivated: bridge.showProject("")
            }

            Item {
                width: parent.width; height: 9
                Rectangle {
                    x: 4; y: 4; width: parent.width - 8; height: 1
                    gradient: Gradient {
                        orientation: Gradient.Horizontal
                        GradientStop { position: 0; color: Theme.alpha(Theme.accent, 0) }
                        GradientStop { position: 0.5; color: Theme.alpha(Theme.accent, 0.45) }
                        GradientStop { position: 1; color: Theme.alpha(Theme.accent, 0) }
                    }
                }
            }

            // SOHBETLER + the selected scope's chip + the round "+".
            Item {
                id: conversationHead
                width: parent.width; height: 32
                readonly property bool allowed: !sidebar.owner.archived && !bridge.busy
                readonly property string reason: sidebar.owner.archived
                    ? qsTr("The project is archived: unarchive it to start a new conversation.")
                    : bridge.busy ? qsTr("Wait for the current run to finish.") : ""
                // The heading gives way (elides) before the chip goes under six
                // characters of the scope's name.
                Text { id: conversationsLabel; x: 10; anchors.verticalCenter: parent.verticalCenter
                       width: Math.max(0, Math.min(implicitWidth, newConversation.x - 8 - 8 - x - scopeChip.floor))
                       elide: Text.ElideRight
                       text: i18n.upper(qsTr("CONVERSATIONS")); color: Theme.textMuted; font: Theme.label
                       HoverTip { objectName: "sidebarConversationsTip"; text: conversationsLabel.text; active: conversationsLabel.truncated } }
                Rectangle {
                    id: scopeChip
                    objectName: "sidebarScopeChip"
                    readonly property bool archived: !!sidebar.owner.archived
                    readonly property color tint: archived ? Theme.warning : Theme.accent
                    // G2, measured as the name is laid out (a hidden Text in its own
                    // font and renderer, not TextMetrics' design advances): seven characters and the
                    // ellipsis - one measured character of headroom, so a slightly
                    // wider face or rasterizer still paints six (1 px past it: an
                    // elide at exactly a string's advance keeps one character fewer).
                    readonly property real floor: 16 + (archived ? 12 : 0) + Math.min(chipName.implicitWidth, Math.ceil(chipSeven.implicitWidth) + 1)
                    readonly property real room: Math.min(Math.max(parent.width * 0.55, floor), newConversation.x - 8 - x)
                    // The ARŞİV pill when it fits beside the floor of the
                    // name; else a warning dot (the tip and name say archived).
                    Text { id: chipSeven; visible: false; font: chipName.font; textFormat: Text.PlainText; text: (sidebar.owner.name || "").slice(0, 7) + "…" }
                    readonly property bool pillFits: archived && room >= archivedPill.width + 6 + Math.ceil(chipSeven.implicitWidth) + 17
                    readonly property real lead: archived ? (pillFits ? archivedPill.width + 6 : 12) : 0
                    readonly property string fullName: (sidebar.owner.name || "") + (archived ? " · " + qsTr("archived") : "")
                    x: conversationsLabel.x + conversationsLabel.width + 8; anchors.verticalCenter: parent.verticalCenter
                    width: Math.max(0, Math.min(room, 16 + lead + chipName.implicitWidth))
                    height: 22; radius: 11
                    color: Theme.alpha(tint, 0.10); border.width: 1; border.color: Theme.alpha(tint, 0.28)
                    Accessible.role: Accessible.StaticText; Accessible.name: fullName
                    Pill { id: archivedPill; objectName: "sidebarScopeArchived"; visible: scopeChip.archived && scopeChip.pillFits
                           x: 3; anchors.verticalCenter: parent.verticalCenter; implicitHeight: 16; dot: false
                           text: qsTr("ARCHIVED"); tint: Theme.warning }
                    Rectangle { visible: scopeChip.archived && !scopeChip.pillFits; x: 8; anchors.verticalCenter: parent.verticalCenter
                                width: 6; height: 6; radius: 3; color: Theme.warning }
                    Text {
                        id: chipName
                        objectName: "sidebarScopeName"
                        x: 8 + scopeChip.lead; anchors.verticalCenter: parent.verticalCenter
                        width: Math.max(0, Math.min(implicitWidth, scopeChip.width - x - 8))
                        text: sidebar.owner.name || ""; textFormat: Text.PlainText; elide: Text.ElideRight
                        color: scopeChip.archived ? Theme.textLabel : Theme.accent; font: Theme.caption
                    }
                    HoverTip { objectName: "sidebarScopeTip"; text: scopeChip.fullName; active: chipName.truncated || scopeChip.archived }
                }
                PlusButton {
                    id: newConversation
                    objectName: "sidebarNewConversation"
                    anchors.right: parent.right; anchors.rightMargin: 2; anchors.verticalCenter: parent.verticalCenter
                    width: 30; height: 30; accent: true
                    allowed: conversationHead.allowed
                    tip: conversationHead.reason || qsTr("New conversation in %1").arg(sidebar.owner.name || "")
                    onActivated: bridge.newConversationInScope()
                }
            }

            // The selected scope's conversations: the one flexible block.
            Flickable {
                id: conversationList
                objectName: "sidebarConversationList"
                width: parent.width; height: sidebar.conversationH
                clip: true; contentHeight: conversationColumn.height
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                // A scope change fades the list in (as the 6E project switch).
                readonly property string scope: sidebar.owner.id || ""
                onScopeChanged: { contentY = 0; if (Theme.ambient) scopeFade.restart() }
                NumberAnimation { id: scopeFade; target: conversationColumn; property: "opacity"; from: 0; to: 1
                                  duration: Theme.panelIn; easing.type: Theme.ease }
                Column {
                    id: conversationColumn
                    width: conversationList.width - (conversationList.contentHeight > conversationList.height ? 10 : 0); spacing: 2
                    Repeater {
                        model: sidebar.groups
                        Column {
                            id: group
                            readonly property string key: modelData.key
                            width: parent.width; spacing: 2
                            Text { objectName: "sidebarGroupLabel"; x: 10; height: sidebar.labelH; verticalAlignment: Text.AlignBottom
                                   bottomPadding: 3; text: modelData.label; color: Theme.textDim; font: Theme.caption }
                            Repeater {
                                model: modelData.items
                                NavRow {
                                    id: conversationRow
                                    objectName: "sidebarConversation"
                                    titleName: "sidebarConversationTitle"; metaName: "sidebarConversationTime"
                                    itemId: modelData.id
                                    title: modelData.title || qsTr("New Chat"); muted: !modelData.title
                                    meta: sidebar.when(modelData, group.key)
                                    current: !!modelData.active
                                    onActivated: bridge.openConversation(modelData.id)
                                    onContextRequested: function (position) {
                                        if (sidebar.conversationMenu) sidebar.conversationMenu.openFor(modelData.id, modelData.title, conversationRow, position)
                                    }
                                }
                            }
                        }
                    }
                    Text {
                        id: emptyLine
                        objectName: "sidebarConversationEmpty"
                        visible: sidebar.groups.length === 0
                        x: 10; width: parent.width - 20; topPadding: 4; bottomPadding: 4
                        wrapMode: Text.Wrap; textFormat: Text.PlainText
                        text: qsTr("No conversation in this scope yet. Press + to start one.")
                        color: Theme.textMuted; font: Theme.caption
                    }
                    // Arşiv (n) (Q10): Genel -> History; a project -> its detail.
                    Item {
                        visible: sidebar.scopeArchived > 0
                        width: parent.width; height: sidebar.linkH + 4
                        NavLink {
                            objectName: "sidebarArchive"
                            anchors.bottom: parent.bottom
                            //: the noun - the archived conversations (a link), not the verb "Archive"
                            text: qsTr("Archive", "sidebar link"); count: String(sidebar.scopeArchived)
                            onActivated: sidebar.owner.global ? bridge.setPage("history") : bridge.showProject(sidebar.owner.id)
                        }
                    }
                }
            }
        }
    }
    SummaryRail {
        id: summaryRail
        x: sidebar.pad; width: sidebar.width - 2 * sidebar.pad
        y: clock.y - 6 - height
        visible: count > 0
        big: sidebar.big
        activeKey: sidebar.activeKey
        onPicked: function(key, cardY, cardHeight) { sidebar.picked(key, summaryRail.y + cardY, cardHeight) }
        onMenuRequested: function(card, anchor, canUp, canDown) { sidebar.menuRequested(card, anchor, canUp, canDown) }
        onApprovalRequested: sidebar.approvalRequested()
    }
    LeftContextCard {
        id: clock
        x: sidebar.pad; width: sidebar.width - 2 * sidebar.pad
        height: 30
        y: sidebar.height - sidebar.pad - height + 4
        big: sidebar.big
        now: sidebar.now
    }
}
