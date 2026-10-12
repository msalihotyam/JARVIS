import QtQuick
import QtQuick.Controls.Basic
import QtQuick.Shapes
import "../theme"

// Gate 6F (L2): the one left panel of the home page. Top to bottom: the
// navigation, the summary tiles (SummaryRail) and the clock strip
// (LeftContextCard). The navigation takes whatever height the tiles and the
// strip leave.
// Gate 6G Sidebar v2 (slice 1, list structure): "Yeni sohbet" (N1), PROJELER
// (L2: the word links to the Projects page, the rest of its row collapses the
// list) over the project TREE (T1-T4: a project row toggles its conversations,
// its right-click menu opens its page, a new conversation, Sabitle, Arşivle),
// "Tüm projeler", then one list: SABİTLENENLER (V3: every owner's pins, a
// folder or a speech bubble says which), the P1 bar that hides the plain
// conversations, and those conversations by day (V1/L3: no "Genel", no
// heading; the list never follows the open conversation). Archived projects
// and conversations are not here (V9). A row pick opens that conversation; a
// right-click on it opens the conversation menu for THAT row (Q8). Collapse
// states are window preferences (bridge.sidebarGroups, bridge.sidebarTree);
// the open conversation stays in view in a collapsed section. It selects,
// grants and approves nothing.
// Slice 2: the L4 row on top (Geri / İleri, the two hide toggles) and V6
// tiles mode (`tiles`: the lists hide, the tiles stack in one column).
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
    // Slice 2: V6 tiles mode (the lists hide, the tiles stack in one column)
    // and the L4 row's state; Main owns the history and the hide mode.
    property bool tiles: false
    property bool canBack: false
    property bool canForward: false
    property string hideMode: ""
    signal picked(string key, real cardY, real cardHeight)
    signal menuRequested(string card, Item anchor, bool canUp, bool canDown)
    signal approvalRequested()
    signal newProjectRequested()
    signal backRequested()
    signal forwardRequested()
    signal hideRequested(string mode)

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
        property string itemId: ""
        property bool current: false
        property bool muted: false
        property bool strong: false
        // A small drawn glyph before the name (V3/V10: folder = a project's
        // conversation, speech bubble = a plain one; N1: the muted "+").
        property string lead: ""
        // F6, a pinned row: the grip on hover over its lead - drag it to
        // reorder; a tip saying where.
        property bool pinned: false
        // DRAG, a project row: the whole row drags (a press and a move past 4 px).
        property bool draggable: false
        property string tip: ""
        property bool dragging: false
        // F7, a conversation row: "⋯" in place of its time (or project) while the
        // row is hovered or focused or its menu is open; it opens that row's menu.
        property bool menuButton: false
        readonly property bool menuHere: !!sidebar.conversationMenu && sidebar.conversationMenu.open && sidebar.conversationMenu.opener === row
        readonly property bool showMenu: menuButton && !dragging && (hovered || activeFocus || menuHere)
        property bool available: !bridge.navigationLocked          // R1: a recorded run leaves rows open
        // R3 (D1/D2): a conversation row starts with its status dot (`dotted`); `gutter` keeps the
        // room for it (a project row too, so the tree's names stay flush, T4). Amber wins over
        // violet, both over green; the bridge never reports the open one as finished.
        property bool dotted: false
        property bool gutter: dotted
        readonly property int inset: gutter ? 12 : 0
        // DFSIDE: a gutter row without a dot (a project row) draws its lead in the
        // dot's column, so the tree's names stay flush.
        readonly property bool leadInGutter: gutter && !dotted && lead !== ""
        readonly property string status: !dotted || itemId === "" ? ""
                                         : bridge.needsYouIds.indexOf(itemId) >= 0 ? "needsYou"
                                         : bridge.runningIds.indexOf(itemId) >= 0 ? "running"
                                         : bridge.queuedIds.indexOf(itemId) >= 0 ? "queued"     // DFQUEUE
                                         : bridge.finishedUnseenIds.indexOf(itemId) >= 0 ? "finished" : ""
        readonly property bool hovered: area.containsMouse || gripArea.containsMouse || menuArea.containsMouse
        signal activated()
        signal contextRequested(point position)
        signal dragStarted()
        signal dragMoved(real sceneY)
        signal dragEnded()
        width: parent ? parent.width : 0
        height: sidebar.rowH
        opacity: dragging ? 0.5 : available ? 1 : 0.38
        radius: Theme.radiusSmall
        color: current ? Theme.alpha(Theme.accent, 0.10) : hovered ? Theme.alpha(Theme.text, 0.05) : "transparent"
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
        activeFocusOnTab: true
        Accessible.role: Accessible.Button
        Accessible.name: [title, statusDot.words, meta].filter(function (part) { return part !== "" }).join(", ")
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
        // D2: a finished row's soft green tint, pulsing slowly with the dot (static with ambient off).
        Rectangle { objectName: "sidebarRowGlow"; visible: row.status === "finished"; anchors.fill: parent; radius: row.radius
                    color: Theme.alpha(Theme.success, 0.12); border.width: 1; border.color: Theme.alpha(Theme.success, 0.40)
                    opacity: statusDot.glow }
        Text {
            id: nameText
            objectName: row.titleName
            x: (row.pinned || row.lead !== "" && !row.leadInGutter ? 28 : 10) + row.inset
            anchors.verticalCenter: parent.verticalCenter
            width: Math.max(0, Math.min(implicitWidth, metaText.x - 8 - x,
                                        row.showMenu ? rowMenu.x - 8 - x : implicitWidth))
            text: row.title; textFormat: Text.PlainText; elide: Text.ElideRight
            color: row.current ? Theme.accent : row.muted ? Theme.textLabel : Theme.text
            font: row.strong ? (sidebar.big ? Theme.pageRowStrong : Theme.bodyStrong) : sidebar.big ? Theme.pageRow : Theme.body
            Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
        }
        Text { id: metaText; objectName: row.metaName; text: row.meta; textFormat: Text.PlainText
               x: row.width - 10 - implicitWidth; anchors.verticalCenter: parent.verticalCenter
               opacity: row.showMenu ? 0 : 1
               color: Theme.textMuted; font: Theme.captionFigures }
        // The lead glyph, or on a pinned row's hover the grip (two columns of three dots).
        Icon { objectName: "sidebarRowLead"; visible: row.lead !== "" && !(row.pinned && (row.hovered || row.dragging))
               x: row.leadInGutter ? 4 : 8 + row.inset; anchors.verticalCenter: parent.verticalCenter; size: 14; name: row.lead
               tint: row.current ? Theme.accent : row.lead === "folder" || row.lead === "project" ? Theme.alpha(Theme.accent, 0.75) : Theme.textLabel }
        Grid {
            objectName: "sidebarPinGrip"
            visible: row.pinned && (row.hovered || row.dragging)
            x: 9 + row.inset; anchors.verticalCenter: parent.verticalCenter; columns: 2; spacing: 3
            Repeater { model: 6; Rectangle { width: 3; height: 3; radius: 1.5; color: gripArea.containsMouse || row.dragging ? Theme.text : Theme.textLabel } }
        }
        Rectangle { objectName: "sidebarRowEdge"; x: 0; y: 8; width: 2; height: row.height - 16; radius: 1; color: Theme.accent
                    opacity: row.current ? 1 : 0
                    Behavior on opacity { NumberAnimation { duration: Theme.hoverMs } } }
        Rectangle { objectName: "sidebarRowFocusRing"; visible: row.activeFocus; anchors.fill: parent; anchors.margins: 1; radius: row.radius - 1
                    color: "transparent"; border.width: 2; border.color: Theme.alpha(Theme.materialGlint, 0.95) }
        MouseArea {
            id: area
            anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.LeftButton | Qt.RightButton
            cursorShape: row.dragging ? Qt.ClosedHandCursor : Qt.PointingHandCursor
            preventStealing: row.draggable
            property real startY: 0
            property bool moved: false
            onPressed: function (mouse) { startY = mouse.y; moved = false }
            onPositionChanged: function (mouse) {
                if (!row.draggable || !(pressedButtons & Qt.LeftButton)) return
                if (!moved && Math.abs(mouse.y - startY) > 4) { moved = true; row.dragStarted() }
                if (row.dragging) row.dragMoved(mapToItem(null, mouse.x, mouse.y).y)
            }
            onReleased: if (row.dragging) row.dragEnded()
            onCanceled: if (row.dragging) row.dragEnded()
            onClicked: function (mouse) {
                if (mouse.button === Qt.RightButton) row.contextRequested(Qt.point(mouse.x, mouse.y))
                else if (!moved && row.available) row.activated()
            }
        }
        // The grip: a press and a move past 4 px drags (the sidebar shows where it
        // drops); a press without a move is a click.
        MouseArea {
            id: gripArea
            objectName: "sidebarPinGripArea"
            visible: row.pinned
            width: 26 + row.inset; height: parent.height
            hoverEnabled: true; preventStealing: true
            cursorShape: row.dragging ? Qt.ClosedHandCursor : Qt.OpenHandCursor
            property real startY: 0
            onPressed: function (mouse) { startY = mouse.y }
            onPositionChanged: function (mouse) {
                if (!pressed) return
                if (!row.dragging && Math.abs(mouse.y - startY) > 4) row.dragStarted()
                if (row.dragging) row.dragMoved(mapToItem(null, mouse.x, mouse.y).y)
            }
            onReleased: if (row.dragging) row.dragEnded(); else if (row.available) row.activated()
            onCanceled: if (row.dragging) row.dragEnded()
        }
        StatusDot { id: statusDot; objectName: "sidebarStatusDot"; visible: row.dotted
                    x: 8; anchors.verticalCenter: parent.verticalCenter; status: row.status }
        Rectangle {
            id: rowMenu
            objectName: "sidebarRowMenuButton"
            visible: row.showMenu
            x: row.width - 6 - width; anchors.verticalCenter: parent.verticalCenter
            width: 24; height: 24; radius: Theme.radiusTiny
            color: menuArea.containsMouse ? Theme.alpha(Theme.text, 0.10) : "transparent"
            Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
            Row {
                anchors.centerIn: parent; spacing: 3
                Repeater { model: 3; Rectangle { width: 3; height: 3; radius: 1.5; color: menuArea.containsMouse ? Theme.text : Theme.textLabel } }
            }
            // Its own click: the row's menu next to the row; the row is not opened.
            MouseArea { id: menuArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                        onClicked: row.contextRequested(Qt.point(row.width + 4, 0)) }
            HoverTip { objectName: "sidebarRowMenuTip"; text: qsTr("Conversation actions"); active: !row.menuHere }
        }
        // A pinned row's whole title is in its info card (V10): its tip only says why it is busy.
        HoverTip { objectName: "sidebarRowTip"; text: !row.available ? busyReason.text : row.tip || row.title
                   active: !row.dragging && !menuArea.containsMouse && !statusDot.hovered
                           && (!row.available || !row.pinned && (row.tip !== "" || nameText.truncated)) }
    }
    // A collapsible heading (SABİTLENENLER, Bugün / Dün / Daha eski): a click,
    // Return or Space toggles it; collapsed it shows its count; the chevron
    // turns (instant with ambient effects off).
    component GroupHead: Item {
        id: head
        property string key: ""
        property string text: ""
        property string labelName: ""
        property int count: 0
        property bool open: true
        property font face: Theme.caption
        function toggle() { bridge.setGroupOpen(head.key, !head.open) }
        width: parent ? parent.width : 0; height: sidebar.labelH
        activeFocusOnTab: true
        Accessible.role: Accessible.Button
        Accessible.name: head.text + ", " + head.count
        Accessible.description: head.open ? qsTr("Expanded") : qsTr("Collapsed")
        Accessible.onPressAction: toggle()
        Keys.onReturnPressed: toggle()
        Keys.onSpacePressed: toggle()
        Row {
            x: 10; height: parent.height; spacing: 6
            Text { objectName: head.labelName; height: parent.height; verticalAlignment: Text.AlignBottom; bottomPadding: 3
                   text: head.text; textFormat: Text.PlainText; font: head.face
                   color: headArea.containsMouse ? Theme.textMuted : Theme.textDim
                   Behavior on color { ColorAnimation { duration: Theme.hoverMs } } }
            Text { objectName: "sidebarGroupCount"; visible: !head.open; height: parent.height; verticalAlignment: Text.AlignBottom; bottomPadding: 3
                   text: "· " + head.count; font: Theme.captionFigures; color: Theme.textDim }
            Chevron { objectName: "sidebarGroupChevron"; y: parent.height - 3 - 12
                      open: head.open; ink: headArea.containsMouse ? Theme.textMuted : Theme.textDim }
        }
        Rectangle { objectName: "sidebarGroupFocusRing"; visible: head.activeFocus; anchors.fill: parent; radius: Theme.radiusTiny
                    color: "transparent"; border.width: 2; border.color: Theme.alpha(Theme.materialGlint, 0.95) }
        MouseArea { id: headArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: head.toggle() }
    }
    // A small text link (PROJELER, Tüm projeler): hover lifts its
    // ink; a tip when it says less than its whole meaning.
    component NavLink: Rectangle {
        id: link
        property string text: ""
        property string tip: ""
        property font face: Theme.caption
        signal activated()
        width: linkRow.width + 16; height: 26; radius: Theme.radiusTiny
        color: linkArea.containsMouse ? Theme.alpha(Theme.text, 0.05) : "transparent"
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
        activeFocusOnTab: true
        Accessible.role: Accessible.Link
        Accessible.name: tip || text
        Accessible.onPressAction: activated()
        Keys.onReturnPressed: activated()
        Keys.onSpacePressed: activated()
        Row {
            id: linkRow
            x: 8; anchors.verticalCenter: parent.verticalCenter; spacing: 8
            Text { visible: link.text !== ""; text: link.text; font: link.face; anchors.verticalCenter: parent.verticalCenter
                   color: linkArea.containsMouse ? Theme.text : Theme.textMuted
                   Behavior on color { ColorAnimation { duration: Theme.hoverMs } } }
        }
        Rectangle { objectName: "sidebarLinkFocusRing"; visible: link.activeFocus; anchors.fill: parent; anchors.margins: 1; radius: link.radius - 1
                    color: "transparent"; border.width: 2; border.color: Theme.alpha(Theme.materialGlint, 0.95) }
        MouseArea { id: linkArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: link.activated() }
        HoverTip { text: link.tip; active: link.tip !== "" }
    }
    // The drawn chevron of a collapsible section: right when collapsed, down
    // when open; it turns over panelIn (at once with ambient effects off).
    component Chevron: Shape {
        id: chevron
        property bool open: true
        property color ink: Theme.textDim
        width: 10; height: 10
        rotation: open ? 90 : 0
        Behavior on rotation { NumberAnimation { duration: Theme.panelIn; easing.type: Theme.ease } }
        preferredRendererType: Shape.CurveRenderer
        ShapePath { strokeColor: chevron.ink; strokeWidth: 1.5; fillColor: "transparent"
                    capStyle: ShapePath.RoundCap; joinStyle: ShapePath.RoundJoin
                    startX: 3.5; startY: 2; PathLine { x: 7; y: 5 } PathLine { x: 3.5; y: 8 } }
    }
    // L4: Geri / İleri (navigation history; disabled when not possible) on the
    // left, the two hide toggles (V5 A "grow", B "keep") on the right. The same
    // row floats over the content while the sidebar is hidden (Main.qml).
    component NavButtons: Item {
        id: navButtons
        property bool canBack: false
        property bool canForward: false
        property string hideMode: ""
        signal backRequested()
        signal forwardRequested()
        signal hideRequested(string mode)
        implicitWidth: 2 * 58 + 16; height: 28
        Row {
            spacing: 2
            IconButton { objectName: "navBack"; icon: "back"; width: 28; height: 28; enabled: navButtons.canBack
                         tip: qsTr("Back"); onClicked: navButtons.backRequested() }
            IconButton { objectName: "navForward"; icon: "forward"; width: 28; height: 28; enabled: navButtons.canForward
                         tip: qsTr("Forward"); onClicked: navButtons.forwardRequested() }
        }
        Row {
            anchors.right: parent.right; spacing: 2
            Repeater {
                model: [{ mode: "grow", tip: qsTr("Hide, widen the chat") }, { mode: "keep", tip: qsTr("Hide, keep the space") }]
                IconButton { objectName: "sidebarHide_" + modelData.mode; icon: "sidebar-" + modelData.mode; width: 28; height: 28
                             active: navButtons.hideMode === modelData.mode
                             tip: active ? qsTr("Show the sidebar") : modelData.tip
                             onClicked: navButtons.hideRequested(active ? "" : modelData.mode) }
            }
        }
    }

    // -- the navigation's data and metrics ------------------------------------
    Text { id: busyReason; visible: false; text: qsTr("Wait for the current run to finish.") }
    readonly property var tree: bridge.sidebarTree
    readonly property var lists: bridge.sidebarConversations
    // V9: active projects only; T2 "Sabitle": the pinned ones first, newest pin on top;
    // the rest in the manual order (DRAG: a new project on top).
    readonly property var projects: {
        var order = bridge.sidebarProjectOrder
        var active = bridge.projectRecords.filter(function (p) { return p.active })
                                          .sort(function (a, b) { return order.indexOf(a.id) - order.indexOf(b.id) })
        var pinnedFirst = []
        for (var i = 0; i < tree.pinned_projects.length; ++i)
            for (var j = 0; j < active.length; ++j) if (active[j].id === tree.pinned_projects[i]) pinnedFirst.push(active[j])
        return pinnedFirst.concat(active.filter(function (p) { return tree.pinned_projects.indexOf(p.id) < 0 }))
    }
    readonly property int rowH: big ? Theme.rowHeight : Theme.menuRowHeight
    readonly property int step: rowH + 2
    readonly property int labelH: 22
    readonly property int linkH: 26
    readonly property int barH: 20
    function count(n) { return n > 0 ? String(n) : "–" }
    // The conversations by the local calendar day of their last change.
    function stamp(v) { return new Date(String(v || "").replace(/(\.\d{3})\d+/, "$1")).getTime() }
    readonly property real today: new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
    readonly property real yesterday: new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1).getTime()
    // F6: the pins (every owner) and which sections are open; a pinned
    // conversation is not repeated in its date group or its project's tree.
    // V9 strict: an archived project's pins are not here either (the Projects
    // page's Arşiv tab keeps them); the pin itself stays.
    readonly property var pins: bridge.pinnedConversations.filter(function (c) { return !c.project_archived })
    readonly property var pinnedIds: { var out = {}; for (var p = 0; p < pins.length; ++p) out[pins[p].id] = true; return out }
    // A drop at `at` among the shown pins, as an index of the whole pin list
    // (hidden ones keep their place relative to the shown).
    function pinIndex(id, at) {
        var all = bridge.pinnedConversations.map(function (c) { return c.id }).filter(function (c) { return c !== id })
        var shown = pins.map(function (c) { return c.id }).filter(function (c) { return c !== id })
        return at < shown.length ? all.indexOf(shown[at]) : shown.length > 0 ? all.indexOf(shown[shown.length - 1]) + 1 : 0
    }
    readonly property var openGroups: bridge.sidebarGroups
    function shownRows(items, open) { return open ? items : items.filter(function (c) { return !!c.active }) }
    // V1: the plain (no-project) conversations, whatever is open; P1 hides them
    // all but the open one.
    readonly property var plain: (lists[""] || []).filter(function (c) { return !pinnedIds[c.id] })
    readonly property var groups: {
        var out = [[], [], []]
        for (var i = 0; i < plain.length; ++i) {
            var t = stamp(plain[i].updated_at)
            out[isNaN(t) || t < yesterday ? 2 : t < today ? 1 : 0].push(plain[i])
        }
        var plainOpen = tree.plain_open
        return [{ key: "today", label: qsTr("Today"), items: out[0] }, { key: "yesterday", label: qsTr("Yesterday"), items: out[1] },
                { key: "earlier", label: qsTr("Earlier"), items: out[2] }].map(function (g) {
            g.shown = plainOpen ? sidebar.shownRows(g.items, sidebar.openGroups[g.key]) : g.items.filter(function (c) { return !!c.active })
            return g
        }).filter(function (g) { return plainOpen ? g.items.length > 0 : g.shown.length > 0 })
    }
    // K3: the one formatter, the tree rows' rule - today its time, else its day.
    function when(c) { return i18n.when(c.updated_at, today, "short") }
    // T1: the project tree, flat - a project row, then (open) its unpinned
    // conversations newest first; a collapsed one keeps only the open one.
    readonly property var treeRows: {
        var out = []
        if (!tree.projects_open) return out
        for (var i = 0; i < projects.length; ++i) {
            var p = projects[i], open = tree.closed.indexOf(p.id) < 0
            out.push({ kind: "project", id: p.id, title: p.name, open: open, count: p.conversation_count || 0,
                       pinned: tree.pinned_projects.indexOf(p.id) >= 0 })
            var rows = shownRows((lists[p.id] || []).filter(function (c) { return !pinnedIds[c.id] }), open)
            for (var k = 0; k < rows.length; ++k)
                out.push({ kind: "conversation", id: rows[k].id, title: rows[k].title, updated_at: rows[k].updated_at, active: rows[k].active })
        }
        return out
    }
    // The tallest run of whole items, from the top, that fits `room`.
    function fit(sizes, room) {
        var h = 0
        for (var k = 0; k < sizes.length; ++k) {
            var next = h + (k > 0 ? 2 : 0) + sizes[k]
            if (next > room) break
            h = next
        }
        return h
    }
    // Height (T3): the fixed rows, then the tree and the list share the rest -
    // each its whole content when both fit, else the shorter keeps its own and
    // the longer the remainder, else half each; whole items only, each its own scroll.
    readonly property real fixedH: searchBox.entryH + 2 + step + 28 + 2 + (tree.projects_open ? linkH + 2 : 0) + 9 + 2
    readonly property real room: navigation.height - fixedH - (treeRows.length > 0 ? 2 : 0)
    readonly property var treeSizes: treeRows.map(function () { return rowH })
    readonly property var listSizes: {
        var sizes = []
        if (pins.length > 0) {
            sizes.push(labelH)
            for (var p = 0; p < shownRows(pins, openGroups.pinned).length; ++p) sizes.push(rowH)
        }
        if (plain.length > 0) sizes.push(barH)
        for (var g = 0; g < groups.length; ++g) {
            sizes.push(labelH)
            for (var i = 0; i < groups[g].shown.length; ++i) sizes.push(rowH)
        }
        return sizes
    }
    readonly property real treeH: {
        var want = fit(treeSizes, Infinity), other = fit(listSizes, Infinity), half = room / 2
        return fit(treeSizes, want + other <= room || want <= half ? want : other <= half ? room - other : half)
    }
    readonly property real listH: fit(listSizes, room - treeH)

    // T2: a project row's right-click menu (the K4 menu style).
    function openProjectMenu(row, position, project) {
        if (conversationMenu) conversationMenu.dismiss()
        projectMenu.project = project
        projectMenu.popupAt(row, position, { flip: true })
    }
    // `plain`: a no-project row (its one-step delete, F7); a tree row is an active project's.
    function openRowMenu(row, position, id, title, plain) {
        projectMenu.close()
        if (conversationMenu) conversationMenu.openFor(id, title, row, position, null, null, false, plain)
    }
    PinInfoCard { id: pinCard }
    // V4 "Yeni proje…" in the New chat screen's picker: the existing route to the
    // new-project dialog (Main: the Projects page, its dialog open).
    Connections { target: bridge; function onNewProjectDialogRequested() { sidebar.newProjectRequested() } }
    MenuSurface {
        id: projectMenu
        objectName: "sidebarProjectMenu"
        property var project: ({})
        readonly property bool pinned: (sidebar.tree.pinned_projects || []).indexOf(project.id) >= 0
        width: 280
        Column {
            width: projectMenu.availableWidth
            spacing: 2
            Text { objectName: "sidebarProjectMenuHeader"; width: parent.width; leftPadding: 12; rightPadding: 12
                   bottomPadding: 4; elide: Text.ElideRight; textFormat: Text.PlainText
                   text: projectMenu.project.title || ""; color: Theme.textMuted; font: Theme.caption }
            ConversationMenu.ScopeChoice { objectName: "sidebarProjectOpenItem"; icon: "project"; label: qsTr("Open project page")
                                           onTriggered: { var id = projectMenu.project.id; projectMenu.close(); bridge.showProject(id) } }
            ConversationMenu.ScopeChoice { objectName: "sidebarProjectNewItem"; icon: "plus"; label: qsTr("New conversation in this project")
                                           onTriggered: { var id = projectMenu.project.id; projectMenu.close()
                                                          if (bridge.newProjectConversation(id)) bridge.setPage("home") } }
            ConversationMenu.ScopeChoice { objectName: "sidebarProjectPinItem"; icon: "pin"
                                           label: projectMenu.pinned ? qsTr("Unpin") : qsTr("Pin")
                                           onTriggered: { var id = projectMenu.project.id, on = !projectMenu.pinned; projectMenu.close()
                                                          bridge.setProjectPinned(id, on) } }
            // R1: not the project of the conversation JARVIS is working for.
            ConversationMenu.ScopeChoice { objectName: "sidebarProjectArchiveItem"; icon: "archive"; label: qsTr("Archive")
                                           available: bridge.runningProjectId !== projectMenu.project.id
                                           onTriggered: { var id = projectMenu.project.id; projectMenu.close(); bridge.archiveProject(id) } }
        }
    }

    NavButtons {
        id: navRow
        objectName: "sidebarNavRow"
        x: sidebar.pad; y: sidebar.pad
        width: sidebar.width - 2 * sidebar.pad
        canBack: sidebar.canBack; canForward: sidebar.canForward; hideMode: sidebar.hideMode
        onBackRequested: sidebar.backRequested()
        onForwardRequested: sidebar.forwardRequested()
        onHideRequested: function (mode) { sidebar.hideRequested(mode) }
    }

    // The navigation region: everything between the L4 row and the tiles.
    Item {
        id: navigation
        objectName: "sidebarNav"
        visible: !sidebar.tiles
        x: sidebar.pad; y: navRow.y + navRow.height + 6
        width: sidebar.width - 2 * sidebar.pad
        height: Math.max(0, (summaryRail.height > 0 ? summaryRail.y - 8 : clock.y - 6) - y)

        Column {
            width: parent.width; spacing: 2

            // F8 (S1): the search entry on top; while a query is active its results
            // take the navigation's place (the rows below hide).
            SidebarSearch {
                id: searchBox
                width: parent.width
                big: sidebar.big; rowH: sidebar.rowH; room: navigation.height
            }

            // N1: "Yeni sohbet" - the New chat screen (a new, empty conversation): in
            // the open conversation's project when that one is writable, else plain.
            NavRow {
                objectName: "sidebarNewChat"
                visible: !searchBox.active
                titleName: "sidebarNewChatName"
                lead: "plus"; title: qsTr("New chat"); muted: true
                // DFSIDE (Salih 2026-10-12): the New chat screen is open - this row is its current row
                // (the blank conversation is listed nowhere, so no conversation row is current).
                current: bridge.newChatOpen
                // V4: in the current project (its page or its conversation) when active, else none.
                onActivated: bridge.startNewChat()
            }

            // L2: the word PROJELER links to the Projects page; the rest of the row
            // collapses / opens the tree (the chevron at the right, the count while collapsed).
            Item {
                id: projectsHead
                visible: !searchBox.active
                width: parent.width; height: 28
                readonly property bool open: sidebar.tree.projects_open
                function toggle() { bridge.setProjectsOpen(!open) }
                NavLink {
                    id: projectsLink
                    objectName: "sidebarProjectsLink"
                    x: 2; anchors.bottom: parent.bottom
                    text: i18n.upper(qsTr("PROJECTS")); face: Theme.label; tip: qsTr("All projects")
                    onActivated: bridge.showProject("")
                }
                Rectangle {
                    id: projectsToggle
                    objectName: "sidebarProjectsToggle"
                    x: projectsLink.x + projectsLink.width + 2; anchors.bottom: parent.bottom
                    width: parent.width - x; height: projectsLink.height; radius: Theme.radiusTiny
                    color: toggleArea.containsMouse ? Theme.alpha(Theme.text, 0.05) : "transparent"
                    Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
                    activeFocusOnTab: true
                    Accessible.role: Accessible.Button
                    Accessible.name: i18n.upper(qsTr("PROJECTS")) + ", " + sidebar.projects.length
                    Accessible.description: projectsHead.open ? qsTr("Expanded") : qsTr("Collapsed")
                    Accessible.onPressAction: projectsHead.toggle()
                    Keys.onReturnPressed: projectsHead.toggle()
                    Keys.onSpacePressed: projectsHead.toggle()
                    Row {
                        anchors.right: parent.right; anchors.rightMargin: 8; anchors.verticalCenter: parent.verticalCenter; spacing: 8
                        Text { objectName: "sidebarProjectsCount"; visible: !projectsHead.open; anchors.verticalCenter: parent.verticalCenter
                               text: String(sidebar.projects.length); font: Theme.captionFigures; color: Theme.textDim }
                        Chevron { objectName: "sidebarProjectsChevron"; anchors.verticalCenter: parent.verticalCenter
                                  open: projectsHead.open; ink: toggleArea.containsMouse ? Theme.textMuted : Theme.textDim }
                    }
                    Rectangle { visible: projectsToggle.activeFocus; anchors.fill: parent; anchors.margins: 1; radius: parent.radius
                                color: "transparent"; border.width: 2; border.color: Theme.alpha(Theme.materialGlint, 0.95) }
                    MouseArea { id: toggleArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                                onClicked: projectsHead.toggle() }
                }
            }

            Flickable {
                id: projectList
                objectName: "sidebarProjectList"
                visible: sidebar.treeH > 0 && !searchBox.active
                width: parent.width; height: sidebar.treeH
                clip: true; contentHeight: projectColumn.height
                // The drawn icons stay inside the clip with every scene-graph backend.
                layer.enabled: true
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                // Whole rows at rest (every tree row is one step).
                onMovementEnded: contentY = Math.min(Math.round(contentY / sidebar.step) * sidebar.step, Math.max(0, contentHeight - height))
                // The open conversation's row stays in view (P3): on open, on a switch, when the tree or
                // its room changes. Only those events move it - never the reader's own scrolling.
                function keepSelected() {
                    var at = -1
                    for (var i = 0; i < sidebar.treeRows.length; ++i) if (sidebar.treeRows[i].active) at = i
                    if (height <= 0) return
                    contentY = Math.min(Math.round(contentY / sidebar.step) * sidebar.step, Math.max(0, contentHeight - height))
                    if (at < 0) return
                    var top = at * sidebar.step, bottom = top + sidebar.step - 2
                    if (top < contentY) contentY = top
                    else if (bottom > contentY + height) contentY = Math.min(Math.max(0, contentHeight - height), Math.max(0, (at + 1 - Math.round((height + 2) / sidebar.step)) * sidebar.step))
                }
                readonly property string selected: bridge.conversationId
                onSelectedChanged: Qt.callLater(keepSelected)
                onHeightChanged: Qt.callLater(keepSelected)
                onContentHeightChanged: Qt.callLater(keepSelected)
                Component.onCompleted: Qt.callLater(keepSelected)
                // DRAG: which project, the pointer (scene y), the tree row it would go
                // above (treeRows.length = the end), the edge it holds at (the tree
                // scrolls under it). Only between unpinned projects: the pinned ones
                // keep their place on top (T2).
                property string dragId: ""
                property int dropAt: -1
                property real pointer: 0
                property int edge: 0
                function track() {
                    var y = mapFromItem(null, 0, pointer).y
                    edge = y > height - 18 ? 1 : y < 18 ? -1 : 0
                    var rows = sidebar.treeRows, at = rows.length
                    var gap = Math.max(0, Math.min(rows.length, Math.round(projectColumn.mapFromItem(null, 0, pointer).y / sidebar.step)))
                    for (var i = gap; i < rows.length; ++i) if (rows[i].kind === "project" && !rows[i].pinned) { at = i; break }
                    dropAt = at
                }
                function drop() {
                    var id = dragId, at = dropAt
                    dragId = ""; dropAt = -1; edge = 0
                    if (id !== "" && at >= 0) bridge.moveProject(id, at < sidebar.treeRows.length ? sidebar.treeRows[at].id : "")
                }
                Timer {
                    interval: 40; repeat: true; running: projectList.dragId !== "" && projectList.edge !== 0
                    onTriggered: {
                        projectList.contentY = Math.max(0, Math.min(projectList.contentHeight - projectList.height,
                                                                    projectList.contentY + projectList.edge * 6))
                        projectList.track()
                    }
                }
                Rectangle {
                    objectName: "sidebarProjectDropLine"
                    z: 1
                    visible: projectList.dragId !== "" && projectList.dropAt >= 0
                    x: 6; width: projectColumn.width - 12; height: 2; radius: 1; color: Theme.accent
                    y: Math.max(0, Math.min(projectColumn.height - height, projectList.dropAt * sidebar.step - 2))
                }
                Column {
                    id: projectColumn
                    width: projectList.width - (projectList.contentHeight > projectList.height ? 10 : 0); spacing: 2
                    Repeater {
                        model: sidebar.treeRows
                        NavRow {
                            id: treeRow
                            readonly property bool isProject: modelData.kind === "project"
                            objectName: isProject ? "sidebarProject" : "sidebarProjectConversation"
                            titleName: isProject ? "sidebarProjectName" : "sidebarProjectConversationTitle"
                            metaName: isProject ? "sidebarProjectCount" : "sidebarProjectConversationTime"
                            itemId: modelData.id
                            title: isProject ? modelData.title : modelData.title || qsTr("New Chat")
                            muted: !isProject && !modelData.title
                            strong: isProject
                            lead: isProject ? "project" : ""          // DFSIDE: a project is told apart at a glance
                            // T4: a project row shows its count only while collapsed; T1 its click
                            // toggles the tree (never busy-gated: nothing runs).
                            // A tree row has no day heading: today's time, else its day (K3).
                            meta: isProject ? (modelData.open ? "" : sidebar.count(modelData.count)) : i18n.when(modelData.updated_at, sidebar.today, "short")
                            available: isProject || !bridge.navigationLocked
                            dotted: !isProject; gutter: true
                            menuButton: !isProject
                            draggable: isProject && !modelData.pinned
                            dragging: isProject && projectList.dragId === modelData.id
                            onDragStarted: { projectList.dragId = modelData.id; projectMenu.close() }
                            onDragMoved: function (sceneY) { projectList.pointer = sceneY; projectList.track() }
                            onDragEnded: projectList.drop()
                            current: !isProject && !!modelData.active
                            Accessible.description: isProject ? (modelData.open ? qsTr("Expanded") : qsTr("Collapsed"))
                                                              : available ? "" : busyReason.text
                            onActivated: isProject ? bridge.setProjectOpen(modelData.id, !modelData.open) : bridge.openConversation(modelData.id)
                            onContextRequested: function (position) {
                                if (treeRow.isProject) sidebar.openProjectMenu(treeRow, position, modelData)
                                else sidebar.openRowMenu(treeRow, position, modelData.id, modelData.title, false)
                            }
                        }
                    }
                }
            }

            NavLink {
                objectName: "sidebarAllProjects"
                visible: sidebar.tree.projects_open && !searchBox.active
                height: sidebar.linkH
                text: qsTr("All projects")
                onActivated: bridge.showProject("")
            }

            Item {
                visible: !searchBox.active
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

            // SABİTLENENLER, the P1 bar and the plain conversations by day.
            Flickable {
                id: conversationList
                objectName: "sidebarConversationList"
                visible: !searchBox.active
                width: parent.width; height: sidebar.listH
                clip: true; contentHeight: conversationColumn.height
                // The drawn glyphs and chevrons stay inside the clip with every scene-graph backend.
                layer.enabled: true
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                Column {
                    id: conversationColumn
                    width: conversationList.width - (conversationList.contentHeight > conversationList.height ? 10 : 0); spacing: 2
                    // SABİTLENENLER (V3): hidden when nothing is pinned. One list for
                    // every owner; the lead glyph says which kind (V10).
                    Column {
                        id: pinSection
                        objectName: "sidebarPinned"
                        visible: sidebar.pins.length > 0
                        width: parent.width; spacing: 2
                        // The drag: which pin, from where, the pointer (scene y), the gap it
                        // would drop into, and whether it holds at the list's top / bottom edge
                        // (-1 / 1: the list scrolls under it, so every gap is reachable).
                        property string dragId: ""
                        property int from: -1
                        property int dropAt: -1
                        property real pointer: 0
                        property int edge: 0
                        function dropIndex(sceneY) {
                            var y = pinColumn.mapFromItem(null, 0, sceneY).y
                            return Math.max(0, Math.min(sidebar.pins.length, Math.round(y / sidebar.step)))
                        }
                        function track() {
                            var y = conversationList.mapFromItem(null, 0, pointer).y
                            edge = y > conversationList.height - 18 ? 1 : y < 18 ? -1 : 0
                            dropAt = dropIndex(pointer)
                        }
                        Timer {
                            interval: 40; repeat: true; running: pinSection.dragId !== "" && pinSection.edge !== 0
                            onTriggered: {
                                conversationList.contentY = Math.max(0, Math.min(conversationList.contentHeight - conversationList.height,
                                                                                 conversationList.contentY + pinSection.edge * 6))
                                pinSection.track()
                            }
                        }
                        function drop() {
                            var id = dragId, start = from, to = dropAt
                            dragId = ""; from = -1; dropAt = -1; edge = 0
                            if (id !== "" && to >= 0 && to !== start && to !== start + 1) bridge.movePin(id, sidebar.pinIndex(id, to > start ? to - 1 : to))
                        }
                        GroupHead {
                            objectName: "sidebarPinnedHead"; labelName: "sidebarPinnedLabel"; key: "pinned"
                            text: i18n.upper(qsTr("PINNED")); face: Theme.label
                            count: sidebar.pins.length; open: !!sidebar.openGroups.pinned
                        }
                        Item {
                            width: parent.width; height: pinColumn.height
                            Column {
                                id: pinColumn
                                width: parent.width; spacing: 2
                                Repeater {
                                    model: sidebar.shownRows(sidebar.pins, sidebar.openGroups.pinned)
                                    NavRow {
                                        id: pinRow
                                        objectName: "sidebarPinnedConversation"
                                        titleName: "sidebarPinnedTitle"
                                        itemId: modelData.id; pinned: true; menuButton: true; dotted: true
                                        lead: modelData.project_id !== "" ? "folder" : "chat"
                                        title: modelData.title || qsTr("New Chat"); muted: !modelData.title
                                        current: !!modelData.active
                                        dragging: pinSection.dragId === modelData.id
                                        // V10: hover and wait - the info card at its right.
                                        onHoveredChanged: if (hovered) pinCard.hoverRow(pinRow, modelData.id); else if (pinCard.row === pinRow) pinCard.leave()
                                        onActivated: { pinCard.leave(); bridge.openConversation(modelData.id) }
                                        onContextRequested: function (position) {
                                            pinCard.leave()
                                            projectMenu.close()
                                            if (sidebar.conversationMenu) sidebar.conversationMenu.openFor(modelData.id, modelData.title, pinRow, position,
                                                                                                           null, null, modelData.project_archived,
                                                                                                           modelData.project_id === "")
                                        }
                                        onDragStarted: { pinCard.leave(); if (sidebar.openGroups.pinned) { pinSection.dragId = modelData.id; pinSection.from = index } }
                                        onDragMoved: function (sceneY) { pinSection.pointer = sceneY; pinSection.track() }
                                        onDragEnded: pinSection.drop()
                                    }
                                }
                            }
                            Rectangle {
                                objectName: "sidebarPinDropLine"
                                visible: pinSection.dragId !== "" && pinSection.dropAt >= 0
                                x: 6; width: parent.width - 12; height: 2; radius: 1; color: Theme.accent
                                y: Math.max(0, Math.min(parent.height - height, pinSection.dropAt * sidebar.step - 2))
                            }
                        }
                    }
                    // P1: a transparent, label-less bar over the plain conversations - a
                    // click hides / shows them all (the open one stays); the chevron on
                    // hover, "n sohbet" while hidden.
                    Item {
                        id: plainBar
                        objectName: "sidebarPlainBar"
                        readonly property bool open: sidebar.tree.plain_open
                        readonly property bool hovered: plainArea.containsMouse || activeFocus
                        readonly property string countText: sidebar.plain.length === 1 ? qsTr("1 conversation")
                                                                                       : qsTr("%1 conversations").arg(sidebar.plain.length)
                        function toggle() { bridge.setPlainOpen(!open) }
                        visible: sidebar.plain.length > 0
                        width: parent.width; height: sidebar.barH
                        activeFocusOnTab: true
                        Accessible.role: Accessible.Button
                        Accessible.name: countText
                        Accessible.description: open ? qsTr("Expanded") : qsTr("Collapsed")
                        Accessible.onPressAction: toggle()
                        Keys.onReturnPressed: toggle()
                        Keys.onSpacePressed: toggle()
                        Row {
                            id: plainTail
                            anchors.right: parent.right; anchors.rightMargin: 8; anchors.verticalCenter: parent.verticalCenter; spacing: 6
                            Text { objectName: "sidebarPlainCount"; visible: !plainBar.open; anchors.verticalCenter: parent.verticalCenter
                                   text: plainBar.countText; textFormat: Text.PlainText; font: Theme.caption; color: Theme.textDim }
                            Chevron { objectName: "sidebarPlainChevron"; anchors.verticalCenter: parent.verticalCenter
                                      opacity: plainBar.hovered ? 1 : 0; open: plainBar.open
                                      ink: plainBar.hovered ? Theme.textMuted : Theme.textDim
                                      Behavior on opacity { NumberAnimation { duration: Theme.hoverMs } } }
                        }
                        Rectangle {
                            x: 6; anchors.verticalCenter: parent.verticalCenter; height: 1
                            width: Math.max(0, (plainBar.open && !plainBar.hovered ? parent.width - 6 : plainTail.x - 6) - x)
                            gradient: Gradient {
                                orientation: Gradient.Horizontal
                                GradientStop { position: 0; color: Theme.alpha(Theme.accent, 0) }
                                GradientStop { position: 0.5; color: Theme.alpha(Theme.accent, plainArea.containsMouse ? 0.5 : 0.32) }
                                GradientStop { position: 1; color: Theme.alpha(Theme.accent, 0) }
                            }
                        }
                        Rectangle { visible: plainBar.activeFocus; anchors.fill: parent; radius: Theme.radiusTiny
                                    color: "transparent"; border.width: 2; border.color: Theme.alpha(Theme.materialGlint, 0.95) }
                        MouseArea { id: plainArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                                    onClicked: plainBar.toggle() }
                        HoverTip { objectName: "sidebarPlainTip"; text: plainBar.open ? qsTr("Hide conversations") : qsTr("Show conversations") }
                    }
                    Repeater {
                        model: sidebar.groups
                        Column {
                            id: group
                            readonly property string key: modelData.key
                            width: parent.width; spacing: 2
                            GroupHead {
                                objectName: "sidebarGroupHead"; labelName: "sidebarGroupLabel"; key: group.key
                                text: modelData.label; count: modelData.items.length; open: !!sidebar.openGroups[group.key]
                            }
                            Repeater {
                                model: modelData.shown
                                NavRow {
                                    id: conversationRow
                                    objectName: "sidebarConversation"; menuButton: true; dotted: true
                                    titleName: "sidebarConversationTitle"; metaName: "sidebarConversationTime"
                                    itemId: modelData.id
                                    title: modelData.title || qsTr("New Chat"); muted: !modelData.title
                                    meta: sidebar.when(modelData)
                                    current: !!modelData.active
                                    onActivated: bridge.openConversation(modelData.id)
                                    onContextRequested: function (position) { sidebar.openRowMenu(conversationRow, position, modelData.id, modelData.title, true) }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    SummaryRail {
        id: summaryRail
        x: sidebar.pad; width: sidebar.width - 2 * sidebar.pad
        // V6: in tiles mode the tiles take the whole height between the rows.
        column: sidebar.tiles
        y: sidebar.tiles ? navRow.y + navRow.height + 8 : clock.y - 6 - height
        height: sidebar.tiles ? Math.max(0, clock.y - 6 - y) : implicitHeight
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
