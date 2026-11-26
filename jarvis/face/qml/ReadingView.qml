import QtQuick
import QtQuick.Templates as T

// FACEREAD1: canvas frame 1, the reading view. It draws face.rows only and calls face slots; it opens nothing.
// Colours and type come from face.tokens; the two layout sizes below are not in the token set yet.
Item {
    id: view
    objectName: "readingView"
    readonly property var tk: face.tokens
    readonly property int columnWidth: 640      // design size reading_col
    readonly property int hitMin: 44            // design size control_min: the smallest clickable side
    readonly property int column: Math.min(columnWidth, width)
    property bool following: true               // the view sticks to the bottom while the user is there
    property bool unseen: false                 // something arrived while the user was scrolled up
    property bool settling: false               // true while the view itself moves to the bottom

    function endY() {                           // the true end, bottom margin included
        return Math.max(list.originY - list.topMargin, list.originY + list.contentHeight + list.bottomMargin - list.height)
    }
    function toEnd() {
        settling = true
        list.positionViewAtEnd()                // creates the last rows; it leaves the bottom margin out
        list.contentY = endY()
        settling = false
        following = true
        unseen = false
    }
    function arrived() {
        if (following)
            Qt.callLater(view.toEnd)
        else
            unseen = true
    }
    function scrollBy(dy) {
        list.contentY = Math.max(list.originY - list.topMargin, Math.min(endY(), list.contentY + dy))
    }
    Component.onCompleted: Qt.callLater(view.toEnd)

    Connections {
        target: face.rows
        function onRowsInserted() { view.arrived() }
        function onDataChanged() { view.arrived() }
        function onModelReset() { view.arrived() }
    }

    ListView {
        id: list
        objectName: "readingList"
        anchors.fill: parent
        interactive: false                      // wheel and scrollbar only: a drag selects text, it never scrolls
        clip: true
        topMargin: view.tk.space_16
        bottomMargin: view.tk.space_24
        model: face.rows
        onContentYChanged: {
            if (view.settling)
                return
            view.following = atYEnd
            if (atYEnd)
                view.unseen = false
        }
        onContentHeightChanged: if (view.following && !view.settling) Qt.callLater(view.toEnd)

        T.ScrollBar.vertical: T.ScrollBar {
            objectName: "readingScroll"
            property string accessibleName: qsTr("Dikey kaydırma çubuğu")
            Accessible.name: accessibleName
            policy: T.ScrollBar.AlwaysOn
            visible: size < 1
            width: view.hitMin
            leftPadding: view.hitMin - view.tk.space_6 - view.tk.space_4
            rightPadding: view.tk.space_4
            contentItem: Rectangle { implicitWidth: view.tk.space_6; radius: width / 2; color: view.tk.color_line2 }
        }

        delegate: Item {
            id: row
            objectName: "readingRow"
            required property int index
            required property string entryId
            required property string kind
            required property string who
            required property string sourceLabel
            required property string time
            required property int voiceSeconds
            required property int readMinutes
            required property var blocks
            readonly property int gap: index === 0 ? 0
                : who === "jarvis" && kind !== "earlier" ? view.tk.space_24 : view.tk.space_12
            width: ListView.view.width
            height: gap + body.height

            Loader {
                id: body
                objectName: "turnBody"
                x: (row.width - view.column) / 2
                y: row.gap
                width: view.column
                sourceComponent: row.kind === "earlier" ? earlier
                    : row.who === "salih" ? salih : row.who === "jarvis" ? jarvis : null
            }
            Component {
                id: salih
                SalihTurn {
                    hitMin: view.hitMin
                    sourceLabel: row.sourceLabel
                    time: row.time
                    voiceSeconds: row.voiceSeconds
                    blocks: row.blocks
                }
            }
            Component {
                id: jarvis
                JarvisTurn {
                    hitMin: view.hitMin
                    entryId: row.entryId
                    time: row.time
                    readMinutes: row.readMinutes
                    blocks: row.blocks
                }
            }
            Component {
                id: earlier
                Text {
                    objectName: "earlierLine"
                    textFormat: Text.PlainText
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.Wrap
                    text: qsTr("Önceki %1 tur · %2 · Göster").arg(row.readMinutes).arg(row.time)
                    color: view.tk.color_text3
                    font.pixelSize: view.tk.type_meta_size
                    lineHeight: view.tk.type_meta_line
                    lineHeightMode: Text.FixedHeight
                }
            }
        }
    }

    WheelHandler {
        objectName: "readingWheel"
        target: null                            // it only scrolls; it never scales or moves the view
        onWheel: (event) => view.scrollBy(-(event.pixelDelta.y !== 0 ? event.pixelDelta.y : event.angleDelta.y / 2))
    }

    Rectangle {
        objectName: "newAnswerPill"
        property string accessibleName: qsTr("Yeni cevap")
        Accessible.role: Accessible.Button
        Accessible.name: accessibleName
        visible: view.unseen && !view.following
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: view.tk.space_12
        height: view.hitMin
        width: Math.max(view.hitMin, pillText.implicitWidth + view.tk.space_20 * 2)
        radius: view.tk.radius_pill
        color: view.tk.color_surface2
        border.color: view.tk.color_accent_line
        border.width: 1
        Text {
            id: pillText
            anchors.centerIn: parent
            text: qsTr("Yeni cevap")
            textFormat: Text.PlainText
            color: view.tk.color_accent
            font.pixelSize: view.tk.type_control_size
        }
        MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: view.toEnd() }
    }
}
