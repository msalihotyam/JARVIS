import QtQuick
import "../theme"

// Quoting v2 (Q2'): the quote popup at the selection - the quoted words as a
// faint preview (left bar, at most two lines), then ONE comment line with the
// paperclip and the add key, the group's thumbnails under it. Enter (or the
// add key) adds the comment; Esc or a press outside closes. The quote itself
// was taken when "Quote" was pressed: a quote this popup started is removed
// again when it closes without being added, so a dismissed popup leaves
// nothing behind. Nothing here sends or authorizes anything.
MenuSurface {
    id: popup
    objectName: "quotePopup"
    property string groupId: ""
    property string excerpt: ""
    property bool fresh: false      // started by this popup: closing without adding removes it
    property bool added: false
    width: 400; padding: 12
    initialFocus: editor.field

    function openFor(anchor, point, gid, text, isNew, note) {
        groupId = gid; excerpt = text; fresh = isNew; added = false
        editor.text = note || ""
        content.forceLayout()                     // measured as it will show, so it flips at the window's edge
        popupAt(anchor, point, { flip: true })
    }
    function add() { editor.commit(); added = true; close() }
    // At the start of the close, not after its fade: a press elsewhere (Send
    // included) must never meet a quote that was dismissed.
    onAboutToHide: {
        var dismissed = fresh && !added ? groupId : ""
        groupId = ""; fresh = false
        if (dismissed !== "") bridge.removeGroup(dismissed)
    }
    // Its quote left the draft (sent, removed from its card): nothing left to add to.
    Connections {
        target: bridge
        function onGroupsChanged() {
            if (popup.visible && popup.groupId !== "" && !(bridge.contextGroups || []).some(function(g) { return g.id === popup.groupId })) popup.close()
        }
    }

    Column {
        id: content
        width: popup.availableWidth
        spacing: 10
        Item {
            width: parent.width; height: preview.height
            Rectangle { width: 2; height: parent.height; radius: 1; color: Theme.alpha(Theme.textMuted, 0.6) }
            Text { id: preview; objectName: "quotePopupExcerpt"; x: 12; width: parent.width - 12
                   text: popup.excerpt.replace(/\s+/g, " "); textFormat: Text.PlainText
                   wrapMode: Text.Wrap; maximumLineCount: 2; elide: Text.ElideRight
                   color: Theme.textMuted; font: Theme.caption }
        }
        Rectangle { width: parent.width; height: 1; color: Theme.glassSeparator }
        QuoteEditor { id: editor; objectName: "quotePopupEditor"; width: parent.width; groupId: popup.groupId; onAccepted: popup.add() }
    }
}
