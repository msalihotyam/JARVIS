import QtQuick
import "../theme"

// Gate 6E: the one list row of History and Projects (G2, G4, G5).
// - Three distinct states: hover is a tonal lift, open / selected is the accent
//   fill with a 2 px edge mark, keyboard focus is the ring (drawn inside the
//   edge: the lists clip their outside). Never colour alone.
// - The name elides at the end and its tip has it whole; `meta` (a date) and
//   the state pill never elide. An archived (`muted`) name reads in textLabel.
// - The actions (the row's children) sit at the right of the name's line; a
//   row too narrow for them beside a 160 px name puts them on a second line.
// - Return / Space activate it; Menu / Shift+F10 ask for its context menu.
Rectangle {
    id: row
    property string title: ""
    property string titleName: ""            // the name Text's objectName (a caller's own name)
    property string meta: ""
    property string detail: ""               // an optional second line
    property string detailName: ""
    property string badge: ""
    property color badgeTint: Theme.accent
    property bool current: false
    property bool muted: false
    default property alias actions: actionRow.data
    signal activated()
    signal contextRequested(point position)

    readonly property real reserved: 12 + (badge !== "" ? pill.width + 8 : 0) + (meta !== "" ? metaText.implicitWidth + 12 : 0)
    readonly property bool stacked: actionRow.width > 0 && actionRow.x - 8 - reserved < 160
    readonly property real lineEnd: stacked || actionRow.width === 0 ? width - 10 : actionRow.x - 8

    width: parent ? parent.width : 0
    height: stacked || detail !== "" ? 64 : Theme.rowHeight
    radius: Theme.radiusSmall
    color: current ? Theme.alpha(Theme.accent, 0.10) : area.containsMouse ? Theme.alpha(Theme.text, 0.05) : "transparent"
    border.width: 1; border.color: current ? Theme.alpha(Theme.accent, 0.6) : Theme.border
    Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
    Behavior on border.color { ColorAnimation { duration: Theme.hoverMs } }

    activeFocusOnTab: true
    Accessible.role: Accessible.Button
    Accessible.name: badge !== "" ? title + ", " + i18n.upper(badge) : title
    Accessible.onPressAction: activated()
    Keys.onPressed: function (event) {
        if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter || event.key === Qt.Key_Space) {
            event.accepted = true; row.activated()
        } else if (event.key === Qt.Key_Menu || (event.key === Qt.Key_F10 && (event.modifiers & Qt.ShiftModifier))) {
            event.accepted = true; row.contextRequested(Qt.point(width / 2, height / 2))
        }
    }
    // A row the keyboard reaches is scrolled into view, in every list it sits in.
    onActiveFocusChanged: if (activeFocus) reveal()
    function reveal() {
        for (var p = parent; p; p = p.parent) {
            if (p.contentY === undefined || !p.contentItem) continue
            var top = row.mapToItem(p.contentItem, 0, 0).y
            if (top < p.contentY) p.contentY = top
            else if (top + row.height > p.contentY + p.height) p.contentY = top + row.height - p.height
        }
    }

    Text {
        id: titleText
        objectName: row.titleName
        x: 12; y: 20 - height / 2
        width: Math.max(0, Math.min(implicitWidth, row.lineEnd - row.reserved))
        text: row.title; textFormat: Text.PlainText; elide: Text.ElideRight
        color: row.current ? Theme.accent : row.muted ? Theme.textLabel : Theme.text; font: Theme.pageRowStrong
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
    }
    Pill { id: pill; objectName: "listRowBadge"; visible: row.badge !== ""; text: row.badge; tint: row.badgeTint
           font: Theme.pageSection; implicitHeight: 24
           x: titleText.x + titleText.width + 8; y: 20 - height / 2 }
    Text { id: metaText; objectName: "listRowMeta"; visible: text !== ""; text: row.meta; textFormat: Text.PlainText
           x: row.lineEnd - implicitWidth; y: 20 - height / 2; color: Theme.textMuted; font: Theme.pageMetaFigures }
    Text { objectName: row.detailName; visible: text !== ""; text: row.detail; textFormat: Text.PlainText
           x: 12; y: 44 - height / 2; width: row.width - 24; elide: Text.ElideRight; color: Theme.textMuted; font: Theme.pageMetaFigures }
    Rectangle { objectName: "listRowEdge"; x: 0; y: 8; width: 2; height: row.height - 16; radius: 1; color: Theme.accent
                opacity: row.current ? 1 : 0
                Behavior on opacity { NumberAnimation { duration: Theme.hoverMs } } }
    Rectangle { objectName: "listRowFocusRing"; visible: row.activeFocus; anchors.fill: parent; anchors.margins: 1; radius: row.radius - 1
                color: "transparent"; border.width: 2; border.color: Theme.alpha(Theme.materialGlint, 0.95) }
    MouseArea {
        id: area
        anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.LeftButton | Qt.RightButton
        cursorShape: Qt.PointingHandCursor
        onClicked: function (mouse) {
            if (mouse.button === Qt.RightButton) row.contextRequested(Qt.point(mouse.x, mouse.y))
            else row.activated()
        }
    }
    HoverTip { objectName: "listRowTip"; text: row.title; active: titleText.truncated }
    Row { id: actionRow; x: row.width - 6 - width; y: (row.stacked ? 44 : 20) - height / 2; spacing: 6 }
}
