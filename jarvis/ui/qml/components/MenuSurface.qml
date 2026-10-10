import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// Gate 6G K4 (app audit, Salih 2026-10-05): THE popup menu surface. Every
// menu in the window is one of these with MenuRow rows:
// - Material: opaque Theme.menuSurface (nothing behind it shows through), one
//   fine edge, Theme.popupRadius; a scrim, if one is ever modal, at
//   Theme.scrimOpacity. An open menu owns the wheel.
// - Keys: focus enters it (`initialFocus`, else its first available row) and
//   Up / Down move through its rows. Esc closes it and returns focus to
//   `opener` (the control it was opened from). `takesFocus: false` leaves the
//   focus where it is (a mouse-only chip whose draft keeps typing).
// - Pointer: a press outside closes it - except a press on its `trigger`, so
//   the trigger's own click toggles it rather than closing and reopening it.
// - Place: popupAt() sets it at a point of an item, kept 8 px inside the
//   window (`flip`: a pointer menu opens to the other side where the window ends);
//   placed again by the same rule whenever its size changes while open (G6MENUFIX).
// - Motion: opacity and Theme.travel over panelIn in, panelOut out; none at
//   all with ambient effects off (K2).
Popup {
    id: surface
    property real restY: 0
    property Item opener: null
    property Item trigger: null
    property Item initialFocus: null
    property bool takesFocus: true
    property string surfaceName: ""
    default property alias content: body.data
    // Where it was declared: its parent again whenever no trigger holds it.
    property Item home: null
    Component.onCompleted: home = parent

    width: 240
    y: restY
    padding: 8
    focus: takesFocus
    closePolicy: Popup.CloseOnEscape | (trigger && parent === trigger ? Popup.CloseOnPressOutsideParent : Popup.CloseOnPressOutside)
    Overlay.modal: Rectangle { color: Qt.rgba(0, 0, 0, Theme.scrimOpacity) }
    background: Rectangle { objectName: surface.surfaceName; radius: Theme.popupRadius; color: Theme.menuSurface
                            border.width: 1; border.color: Theme.glassOverlayEdge
                            WheelHandler { } }
    contentItem: FocusScope {
        id: body
        focus: true                                  // holds the focus when no row does: Esc stays the menu's
        implicitHeight: childrenRect.height
        Keys.onShortcutOverride: function (event) { if (event.key === Qt.Key_Escape) event.accepted = true }
        Keys.onEscapePressed: surface.escapeClose()
        Keys.onUpPressed: surface.step(-1)
        Keys.onDownPressed: surface.step(1)
        // The window's page shortcuts pass through an open menu (G6K4FIX2). Qt
        // blocks every window Shortcut outside a popup that closes on Escape, so
        // while open the menu holds the window's `menuShortcuts` itself (inside
        // the popup, where the window's own are blocked while it shows): one closes
        // the menu and fires. The target is read first: close() releases this mirror.
        Instantiator {
            model: surface.visible && body.Window.window ? body.Window.window.menuShortcuts || [] : []
            delegate: Shortcut {
                required property var modelData
                sequence: modelData.sequence
                enabled: modelData.enabled
                onActivated: { var target = modelData; surface.close(); target.activated() }
            }
        }
    }

    enter: Theme.ambient ? menuIn : null
    exit: Theme.ambient ? menuOut : null
    readonly property Transition menuIn: Transition {
        ParallelAnimation {
            NumberAnimation { property: "opacity"; from: 0; to: 1; duration: Theme.panelIn; easing.type: Theme.ease }
            NumberAnimation { property: "y"; from: surface.restY - Theme.travel; to: surface.restY; duration: Theme.panelIn; easing.type: Theme.ease }
        }
    }
    readonly property Transition menuOut: Transition {
        NumberAnimation { property: "opacity"; to: 0; duration: Theme.panelOut; easing.type: Theme.ease }
    }
    // G6F1: re-bind y, never assign it: an assignment broke `y: restY`, and only
    // menuIn (ambient on) wrote y again, so motion-free the next open kept the last place.
    onClosed: { opacity = 1; y = Qt.binding(() => restY) }
    // G6MENUFIX: a row shown or hidden after popupAt (its layout runs at the next
    // polish) changes the size: place it again. menuIn ends on the restY it started
    // with, so the open menu takes the current one.
    onImplicitHeightChanged: if (visible) place()
    onWidthChanged: if (visible) place()
    onOpened: y = Qt.binding(() => restY)
    onAboutToShow: if (!opener) opener = body.Window.activeFocusItem
    onVisibleChanged: if (visible && takesFocus) {
        var first = initialFocus || rows()[0]
        if (first) first.forceActiveFocus(Qt.PopupFocusReason)
    }

    // Open at `point` of `anchor`. `how`: { trigger, opener (default: the
    // trigger, else the anchor), flip }.
    function popupAt(anchor, point, how) {
        how = how || {}
        trigger = how.trigger || null
        opener = how.opener || trigger || anchor
        parent = trigger || home
        placed = { win: anchor.Window.window, at: anchor.mapToItem(null, point.x, point.y), flip: !!how.flip }
        place()
        open()
    }
    // The last popupAt's window point: where place() keeps it.
    property var placed: null
    function place() {
        if (!placed) return
        var win = placed.win, at = placed.at, flip = placed.flip
        var w = width, h = implicitHeight
        var ww = win ? win.width : 1920, wh = win ? win.height : 1080
        var sx = flip && at.x + w > ww - 8 ? at.x - w : at.x
        var sy = flip && at.y + h > wh - 8 ? at.y - h : at.y
        var p = parent.mapFromItem(null, Math.max(8, Math.min(sx, ww - w - 8)), Math.max(8, Math.min(sy, wh - h - 8)))
        x = p.x; restY = p.y
    }
    function escapeClose() {
        var back = opener
        close()
        if (back && back.visible) back.forceActiveFocus()
    }
    // Its available rows, top to bottom (a list's rows included).
    function rows() {
        var found = [], stack = [body]
        while (stack.length) {
            var node = stack.pop()
            for (var i = 0; i < node.children.length; ++i) {
                var child = node.children[i]
                if (child.menuRow === true) { if (child.visible && child.enabled) found.push(child) }
                else stack.push(child)
            }
        }
        return found.sort(function (a, b) { return a.mapToItem(body, 0, 0).y - b.mapToItem(body, 0, 0).y })
    }
    function step(by) {
        var list = rows()
        if (!list.length) return
        var at = list.indexOf(body.Window.activeFocusItem)
        list[at < 0 ? (by > 0 ? 0 : list.length - 1) : (at + by + list.length) % list.length].forceActiveFocus(Qt.TabFocusReason)
    }
}
