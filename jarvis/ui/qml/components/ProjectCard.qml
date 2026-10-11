import QtQuick
import "../theme"

// Gate 6G P2: one project on the Projects page's card grid - its name (a pin
// mark when pinned, an "in archive" pill when archived), the first line of its
// stored instruction and its last activity, as the page hands them over (K3: nothing invented;
// no instruction says so). A click opens the project; the "⋯" asks the page
// for the card's menu (K4: the page's one MenuSurface).
Rectangle {
    id: card
    objectName: "projectCard"
    property var project: ({})
    property string lastActivity: ""
    property bool pinned: false
    readonly property bool archived: project.active === false
    signal opened()
    signal menuRequested(Item anchor)

    implicitHeight: 128
    radius: Theme.radiusSmall
    color: area.containsMouse ? Theme.alpha(Theme.text, 0.05) : Theme.glassNested
    Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
    border.width: 1
    border.color: activeFocus ? Theme.glassEdgeStrong : area.containsMouse ? Theme.alpha(Theme.accent, 0.45) : Theme.border
    activeFocusOnTab: true
    Accessible.role: Accessible.Button
    Accessible.name: project.name || ""
    Accessible.onPressAction: card.opened()
    Keys.onReturnPressed: card.opened()
    Keys.onSpacePressed: card.opened()

    MouseArea { id: area; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: card.opened() }

    Column {
        x: 16; y: 14; width: parent.width - x - menuButton.width - 10
        spacing: 6
        Row {
            width: parent.width; spacing: 8
            Text { id: nameText; objectName: "projectCardName"; anchors.verticalCenter: parent.verticalCenter
                   width: Math.min(implicitWidth, parent.width - (pinMark.visible ? pinMark.width + 8 : 0)
                                                    - (archivedPill.visible ? archivedPill.width + 8 : 0))
                   text: card.project.name || ""; textFormat: Text.PlainText; elide: Text.ElideRight
                   color: card.archived ? Theme.textMuted : Theme.text; font: Theme.pageRowStrong
                   HoverTip { text: nameText.text; active: nameText.truncated } }
            Icon { id: pinMark; objectName: "projectCardPin"; visible: card.pinned; anchors.verticalCenter: parent.verticalCenter
                   name: "pin"; size: 13; tint: Theme.textLabel }
            Pill { id: archivedPill; objectName: "projectCardArchived"; visible: card.archived; anchors.verticalCenter: parent.verticalCenter
                   text: qsTr("In archive"); tint: Theme.warning; dot: false }
        }
        Text { objectName: "projectCardInstruction"; width: parent.width; wrapMode: Text.Wrap; maximumLineCount: 2; elide: Text.ElideRight
               textFormat: Text.PlainText; font: Theme.pageMeta
               text: card.project.instruction_line || qsTr("No instruction")
               color: Theme.textMuted }
    }
    Text { objectName: "projectCardActivity"; x: 16; anchors.bottom: parent.bottom; anchors.bottomMargin: 12
           width: parent.width - 32; elide: Text.ElideRight; textFormat: Text.PlainText
           text: card.lastActivity; color: Theme.textLabel; font: Theme.captionFigures }
    IconButton { id: menuButton; objectName: "projectCardMenu"; anchors.top: parent.top; anchors.right: parent.right; anchors.margins: 8
                 width: 30; height: 30; icon: "more"; tip: qsTr("Project actions"); onClicked: card.menuRequested(menuButton) }
}
