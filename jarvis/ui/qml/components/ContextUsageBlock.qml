import QtQuick
import "../theme"

// V2.1a M4 (MEMUICTX): the open conversation's context use, inside the usage
// popover - "used / max" tokens over a bar filled to the app's own ratio, the
// turns left before the automatic compaction, and "Oturumu sıkıştır". Nothing
// is computed here: an unknown window says so (K3), never a guessed figure.
//
// Bridge-independent: `usage` is bridge.contextUsage's shape
// {used, max, ratio, turnsUntilAutoCompact, state}; `busy` (a reply streaming)
// and `canCompact` (a conversation is open) gate the button; `compact()` asks.
Column {
    id: block
    objectName: "contextUsageBlock"
    property var usage: ({})
    property bool busy: false
    property bool canCompact: true
    signal compact()
    spacing: 6

    function known(value) { return typeof value === "number" && isFinite(value) }
    function count(value) { return Number(value).toLocaleString(Qt.locale(i18n.qtLocale), "f", 0) }
    readonly property var u: usage || ({})
    readonly property string figure: known(u.used) && known(u.max) ? qsTr("%1 / %2 tokens").arg(count(u.used)).arg(count(u.max))
                                   : known(u.used) ? qsTr("%1 tokens / limit unknown").arg(count(u.used))
                                   : qsTr("Unknown")
    readonly property string turns: !known(u.turnsUntilAutoCompact) ? ""
        : (u.turnsUntilAutoCompact === 1 ? qsTr("%1 turn until auto-compaction") : qsTr("%1 turns until auto-compaction"))
            .arg(u.turnsUntilAutoCompact)

    Item { width: parent.width; height: title.height
        Text { id: title; text: qsTr("Context usage"); color: Theme.text; renderType: Theme.textRender; font: Theme.captionStrong }
        Text { objectName: "contextUsageFigure"; anchors.right: parent.right; anchors.baseline: title.baseline
               text: block.figure; color: Theme.text; renderType: Theme.textRender; font: Theme.captionFigures } }
    Rectangle { objectName: "contextUsageTrack"; visible: block.known(block.u.ratio)
                width: parent.width; height: 5; radius: 2.5; color: Theme.alpha(Theme.accent, 0.16)
        Rectangle { objectName: "contextUsageBar"; width: parent.width * Math.max(0, Math.min(1, block.u.ratio || 0))
                    height: parent.height; radius: parent.radius; color: Theme.accent } }
    Item { width: parent.width; height: compactButton.height
        Text { objectName: "contextUsageTurns"; anchors.verticalCenter: parent.verticalCenter
               width: parent.width - compactButton.width - 8; wrapMode: Text.Wrap
               visible: text !== ""; text: block.turns
               color: Theme.textMuted; renderType: Theme.textRender; font: Theme.caption }
        ActionButton { id: compactButton; objectName: "contextCompactButton"; anchors.right: parent.right; compact: true
                       label: qsTr("Compact session"); enabled: !block.busy && block.canCompact
                       onClicked: block.compact() } }
}
