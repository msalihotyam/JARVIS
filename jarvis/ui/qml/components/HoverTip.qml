import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// Gate 6E (G3): the one hover tip - the full text of an elided name, or why a
// control is disabled. It covers `target` (default: its parent) and shows
// below it after Theme.tipDelay, in the existing tip style. The tip itself is a
// popup in the window's overlay, so it never inherits a dimmed owner's
// opacity. A DISABLED owner gets no hover events, nor do its children: give
// such an owner a SIBLING tip with `target: owner`. At most 480 px wide, it
// wraps (names fit in 3 lines) and never elides.
// The hover sensor sits ON the target while it is enabled, so the owner's own
// hover (a MouseArea's containsMouse) is shared, not taken; on a disabled
// target it sits on this sibling, which covers it.
Item {
    id: tip
    property Item target: parent
    property string text: ""
    property bool active: true
    // Where the tip sits, from the target's top-left: by default just below it.
    property real offsetX: 0
    property real offsetY: height + 8
    readonly property bool shown: popup.visible
    // The tip's own width, so an owner can centre it (offsetX: (width - tipWidth) / 2).
    readonly property real tipWidth: popup.width
    anchors.fill: target

    HoverHandler { id: hover; parent: tip.target && tip.target.enabled ? tip.target : tip }
    TextMetrics { id: metrics; font: Theme.caption; text: tip.text }

    ToolTip {
        id: popup
        objectName: "hoverTipPopup"
        x: tip.offsetX; y: tip.offsetY
        delay: Theme.tipDelay
        visible: hover.hovered && tip.active && tip.text !== ""
        leftPadding: 8; rightPadding: 8; topPadding: 4; bottomPadding: 4
        width: Math.min(Math.ceil(metrics.advanceWidth) + leftPadding + rightPadding + 1, 480)
        background: Rectangle { radius: Theme.radiusTiny; color: Theme.surfaceRaised; border.width: 1; border.color: Theme.borderStrong }
        contentItem: Text {
            objectName: "hoverTipText"
            text: tip.text; textFormat: Text.PlainText
            wrapMode: Text.Wrap; elide: Text.ElideNone
            color: Theme.text; font: Theme.caption
            lineHeightMode: Text.FixedHeight; lineHeight: 16
        }
    }
}
