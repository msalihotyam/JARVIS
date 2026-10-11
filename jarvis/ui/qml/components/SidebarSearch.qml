import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// Gate 6G F8 (S1-S9): the sidebar's conversation search. On top of the
// navigation a field-like "Sohbetlerde ara" button; one click turns it into
// the field, its X closes it and the normal list returns (Esc / Return are
// extras only). From two characters on, after a short pause in typing, the
// bridge scans the stored conversations off the window's thread
// (jarvis.conversation_search) and the results take the navigation's place:
// BAŞLIKLAR (name matches), then İÇERİKLER (text matches with a snippet, the
// hit highlighted; "eski özet" on a digest hit), each with its faint scope.
// "Aranıyor…" only while a scan really runs; "Sonuç yok" when none matched;
// at the cap, "daha fazla sonuç var". A result opens its conversation like a
// pin does. The search reads only: it selects, writes and grants nothing.
Item {
    id: search
    objectName: "sidebarSearch"
    property bool big: true
    property int rowH: Theme.rowHeight
    // The navigation's height: the results take it while a query is active.
    property real room: 0
    property bool open: false
    readonly property string query: open ? field.text.trim().replace(/\s+/g, " ") : ""
    readonly property bool active: query.length >= 2
    property bool searching: false
    property var result: null
    readonly property int entryH: big ? 38 : 32
    readonly property bool empty: !!result && !result.failed && result.titles.length + result.contents.length === 0
    height: active ? Math.max(entryH, room) : entryH

    function begin() { open = true; field.forceActiveFocus() }
    function close() { debounce.stop(); field.text = ""; result = null; searching = false; open = false }
    function run() { debounce.stop(); searching = true; bridge.searchConversations(query) }

    // A new query drops the old answer (no stale rows under new words); the scan
    // starts once typing pauses.
    onQueryChanged: { result = null; searching = false; if (active) debounce.restart(); else debounce.stop() }
    Timer { id: debounce; interval: 250; onTriggered: search.run() }
    Connections {
        target: bridge
        function onConversationSearchDone(found) {
            if (!search.active || found.query !== search.query) return
            search.result = found
            search.searching = false
        }
        // A rename, delete or archive anywhere: the shown answer is scanned again (kept until the new one).
        function onConversationChanged() { if (search.active) debounce.restart() }
    }

    // -- the entry: the button, or (open) the field with its X ----------------
    Rectangle {
        id: entry
        objectName: "sidebarSearchEntry"
        width: parent.width; height: search.entryH
        radius: Theme.radiusSmall
        color: Theme.surfaceDeep
        border.width: 1
        border.color: field.activeFocus ? Theme.commandEdge : entryArea.containsMouse ? Theme.alpha(Theme.accent, 0.45) : Theme.border
        Behavior on border.color { ColorAnimation { duration: Theme.hoverMs } }
        activeFocusOnTab: !search.open
        Accessible.role: Accessible.Button
        Accessible.name: qsTr("Search conversations")
        Accessible.onPressAction: search.begin()
        Keys.onReturnPressed: search.begin()
        Keys.onSpacePressed: search.begin()
        Icon { x: 11; anchors.verticalCenter: parent.verticalCenter; size: 15; name: "search"; tint: Theme.textMuted }
        Text {
            objectName: "sidebarSearchLabel"
            visible: !search.open
            x: 34; anchors.verticalCenter: parent.verticalCenter
            width: parent.width - x - 10; elide: Text.ElideRight
            text: qsTr("Search conversations"); textFormat: Text.PlainText
            color: Theme.textMuted; font: search.big ? Theme.pageRow : Theme.body
        }
        MouseArea { id: entryArea; anchors.fill: parent; visible: !search.open; hoverEnabled: true
                    cursorShape: Qt.IBeamCursor; onClicked: search.begin() }
        Rectangle { visible: entry.activeFocus; anchors.fill: parent; anchors.margins: 1; radius: parent.radius - 1
                    color: "transparent"; border.width: 2; border.color: Theme.alpha(Theme.materialGlint, 0.95) }
        TextField {
            id: field
            objectName: "sidebarSearchField"
            visible: search.open
            x: 28; width: closeButton.x - x - 2; height: parent.height
            leftPadding: 6; rightPadding: 4; topPadding: 0; bottomPadding: 0
            verticalAlignment: TextInput.AlignVCenter
            selectByMouse: true; maximumLength: 200
            color: Theme.text; font: search.big ? Theme.pageRow : Theme.body
            placeholderText: qsTr("Search conversations"); placeholderTextColor: Theme.textDim
            background: Item {}
            Keys.onEscapePressed: search.close()
            Keys.onReturnPressed: if (search.active) search.run()
            Keys.onEnterPressed: if (search.active) search.run()
        }
        IconButton {
            id: closeButton
            objectName: "sidebarSearchClose"
            visible: search.open
            x: parent.width - width - 4; anchors.verticalCenter: parent.verticalCenter
            width: search.entryH - 8; height: width
            icon: "close"; tip: qsTr("Close search")
            onClicked: search.close()
        }
    }

    // -- the results, in the navigation's place ---------------------------------
    // One result row: the name and its faint scope; a content hit adds its snippet.
    component ResultRow: Rectangle {
        id: row
        property var item: ({})
        property bool content: false
        readonly property bool available: !bridge.navigationLocked
        readonly property bool current: row.item.id === bridge.conversationId
        readonly property string name: row.item.title || (row.item.legacy ? qsTr("Previous conversation") : qsTr("New Chat"))
        // V1: no "Genel" in the UI - a plain chat shows no scope, a project chat its project.
        readonly property string scope: row.item.project_id ? (row.item.project_name || "") : ""
        function pick() { if (available) bridge.openConversation(row.item.id) }
        width: parent ? parent.width : 0
        height: content ? search.rowH + 18 : search.rowH
        radius: Theme.radiusSmall
        opacity: available ? 1 : 0.38
        color: current ? Theme.alpha(Theme.accent, 0.10) : area.containsMouse ? Theme.alpha(Theme.text, 0.05) : "transparent"
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
        activeFocusOnTab: true
        Accessible.role: Accessible.Button
        Accessible.name: scope ? name + ", " + scope : name
        Accessible.onPressAction: pick()
        Keys.onReturnPressed: pick()
        Keys.onSpacePressed: pick()
        Text {
            id: nameText
            objectName: "sidebarSearchTitle"
            x: 10; y: row.content ? 6 : (row.height - height) / 2
            width: Math.max(0, Math.min(implicitWidth, scopeText.x - 8 - x))
            text: row.name; textFormat: Text.PlainText; elide: Text.ElideRight
            color: row.current ? Theme.accent : row.item.title ? Theme.text : Theme.textLabel
            font: search.big ? Theme.pageRow : Theme.body
        }
        Text {
            id: scopeText
            objectName: "sidebarSearchScope"
            width: Math.min(implicitWidth, row.width * 0.4)
            x: row.width - 10 - width; anchors.verticalCenter: nameText.verticalCenter
            text: row.scope; textFormat: Text.PlainText; elide: Text.ElideRight
            color: Theme.textDim; font: Theme.caption
        }
        Item {
            id: snippet
            objectName: "sidebarSearchSnippet"
            visible: row.content
            x: 10; y: nameText.y + nameText.height + 2
            width: row.width - 20; height: hitText.implicitHeight
            Text { id: markText; objectName: "sidebarSearchDigest"; visible: !!row.item.digest
                   width: visible ? implicitWidth + 6 : 0
                   text: qsTr("old summary"); textFormat: Text.PlainText; color: Theme.textDim; font: Theme.caption }
            Text { id: beforeText; objectName: "sidebarSearchBefore"; x: markText.width
                   width: Math.min(implicitWidth, Math.max(0, (snippet.width - x - hitText.width) * 0.4))
                   text: row.item.before || ""; textFormat: Text.PlainText; elide: Text.ElideLeft
                   color: Theme.textMuted; font: Theme.caption }
            Text { id: hitText; objectName: "sidebarSearchHit"; x: beforeText.x + beforeText.width
                   width: Math.min(implicitWidth, Math.max(0, snippet.width - x))
                   text: row.item.hit || ""; textFormat: Text.PlainText; elide: Text.ElideRight
                   color: Theme.accent; font: Theme.captionStrong }
            Text { objectName: "sidebarSearchAfter"; x: hitText.x + hitText.width
                   width: Math.max(0, snippet.width - x)
                   text: row.item.after || ""; textFormat: Text.PlainText; elide: Text.ElideRight
                   color: Theme.textMuted; font: Theme.caption }
        }
        Rectangle { objectName: "sidebarSearchFocusRing"; visible: row.activeFocus; anchors.fill: parent; anchors.margins: 1
                    radius: row.radius - 1; color: "transparent"; border.width: 2; border.color: Theme.alpha(Theme.materialGlint, 0.95) }
        MouseArea { id: area; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: row.pick() }
        HoverTip { objectName: "sidebarSearchTip"
                   text: !row.available ? qsTr("Wait for the current run to finish.") : row.name + "\n" + row.scope
                   active: !row.available || nameText.truncated || scopeText.truncated }
    }
    component Heading: Text {
        width: parent ? parent.width : 0; height: 22
        leftPadding: 10; bottomPadding: 3; verticalAlignment: Text.AlignBottom
        textFormat: Text.PlainText; font: Theme.label; color: Theme.textDim
    }
    component Note: Text {
        width: parent ? parent.width : 0
        leftPadding: 10; rightPadding: 10; topPadding: 8; bottomPadding: 8; wrapMode: Text.Wrap
        textFormat: Text.PlainText; font: Theme.caption; color: Theme.textMuted
    }

    Flickable {
        id: results
        objectName: "sidebarSearchResults"
        visible: search.active
        y: entry.height + 6
        width: parent.width; height: Math.max(0, search.height - y)
        clip: true; contentHeight: resultColumn.height
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
        Column {
            id: resultColumn
            width: results.width - (results.contentHeight > results.height ? 10 : 0); spacing: 2
            Note { objectName: "sidebarSearchStatus"
                   visible: text !== ""
                   text: search.result && search.result.failed ? qsTr("The search could not be run.")
                       : search.searching && !search.result ? qsTr("Searching…")
                       : search.empty ? qsTr("No results") : "" }
            Heading { objectName: "sidebarSearchTitlesHead"; visible: !!search.result && search.result.titles.length > 0
                      text: i18n.upper(qsTr("TITLES")) }
            Repeater {
                model: search.result ? search.result.titles : []
                ResultRow { objectName: "sidebarSearchTitleRow"; item: modelData }
            }
            Heading { objectName: "sidebarSearchContentsHead"; visible: !!search.result && search.result.contents.length > 0
                      text: i18n.upper(qsTr("CONTENTS")) }
            Repeater {
                model: search.result ? search.result.contents : []
                ResultRow { objectName: "sidebarSearchContentRow"; item: modelData; content: true }
            }
            Note { objectName: "sidebarSearchMore"; visible: !!search.result && search.result.more
                   text: qsTr("There are more results") }
        }
    }
}
