import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// Gate 6E: the one modal dialog shell (attach, delete, purge move onto it).
// A caller gives the header text, the body (its default content) and the
// footer `buttons`; layering, size, keys and motion are decided here once.
// - Scrim: black at Theme.scrimOpacity, so the rows behind recede rather than
//   compete with the dialog's edge.
// - Size: at most the window minus 80 px. The body scrolls; the footer never
//   leaves the screen.
// - Keys: Esc cancels. Tab stays inside (a modal popup is a tab fence). Focus
//   starts on `initialFocus` - the SAFE control - and returns to the opener.
//   Enter acts only on the focused button: a field confirms nothing unless the
//   dialog opts in with `enterConfirms`, and a field in `enterGuard` never does.
// - Motion: ConfirmDialog's (fade + a scale whose edges travel at most
//   Theme.travel, over pageMs in, panelOut out). A destructive dialog
//   (`danger`) has no motion at all (K2, the effect plan's no-effect list),
//   nor does any dialog with ambient effects off. `confirmItem` is enabled
//   only while the dialog is fully open, never during either motion.
Popup {
    id: dialog
    property string label: ""                 // the small UPPERCASE line beside the dot
    property string title: ""
    property string titleName: "jarvisDialogTitle"      // the title Text's objectName (a caller's own name)
    property bool danger: false
    property int preferredWidth: 600
    property Item initialFocus: null
    property Item confirmItem: null
    property bool confirmEnabled: true         // the caller's own condition for confirmItem
    property bool enterConfirms: false
    property var enterGuard: []
    property Item opener: null
    default property alias body: bodyColumn.data
    property alias buttons: footerRow.data
    readonly property color tone: danger ? Theme.danger : Theme.accent

    // Remembers who opened it (else the item that had focus), for focus return.
    function openFrom(item) { opener = item || null; open() }

    parent: Overlay.overlay
    anchors.centerIn: parent
    modal: true; focus: true
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
    padding: 0
    width: Math.min(preferredWidth, parent ? parent.width - 80 : preferredWidth)
    height: Math.min(frame.naturalHeight, parent ? parent.height - 80 : frame.naturalHeight)

    Overlay.modal: Rectangle {
        objectName: "jarvisDialogScrim"
        color: Qt.rgba(0, 0, 0, Theme.scrimOpacity)
        Behavior on opacity { enabled: dialog.moves; NumberAnimation { duration: Theme.pageMs } }
    }

    readonly property bool moves: Theme.ambient && !danger
    enter: moves ? enterMotion : null
    exit: moves ? exitMotion : null
    readonly property Transition enterMotion: Transition {
        ParallelAnimation {
            NumberAnimation { property: "opacity"; from: 0; to: 1; duration: Theme.pageMs; easing.type: Theme.ease }
            NumberAnimation { property: "scale"; from: 1 - 2 * Theme.travel / Math.max(1, dialog.width, dialog.height); to: 1; duration: Theme.pageMs; easing.type: Theme.ease }
        }
    }
    readonly property Transition exitMotion: Transition {
        NumberAnimation { property: "opacity"; to: 0; duration: Theme.panelOut; easing.type: Theme.ease }
    }

    Binding { target: dialog.confirmItem; property: "enabled"; value: dialog.opened && dialog.confirmEnabled
              when: dialog.confirmItem !== null }

    onAboutToShow: if (!opener) opener = parent ? parent.Window.activeFocusItem : null
    onVisibleChanged: if (visible && initialFocus) initialFocus.forceActiveFocus()
    onClosed: {
        opacity = 1; scale = 1                 // an exit motion never leaves the next open faded
        var back = opener
        opener = null
        if (back) back.forceActiveFocus()
    }

    background: Rectangle {
        radius: 12
        color: Theme.surfaceRaised
        border.width: 1; border.color: Theme.alpha(dialog.tone, 0.7)
    }

    contentItem: FocusScope {
        id: frame
        readonly property real naturalHeight: header.y + header.height + 14 + bodyColumn.height + footerArea.height
        // Return / Enter that no focused button took: never leaves the dialog.
        Keys.onPressed: function (event) {
            if (event.key !== Qt.Key_Return && event.key !== Qt.Key_Enter) return
            event.accepted = true
            if (dialog.enterGuard.indexOf(frame.Window.activeFocusItem) >= 0) return
            var confirm = dialog.confirmItem
            if (dialog.enterConfirms && confirm && confirm.visible && confirm.enabled) confirm.clicked()
        }

        Column {
            id: header
            x: 22; y: 20; width: frame.width - 44; spacing: 8
            Row {
                visible: dialog.label !== ""
                spacing: 12
                Rectangle {
                    width: 10; height: 10; radius: 5; color: dialog.tone; anchors.verticalCenter: parent.verticalCenter
                    Rectangle { anchors.centerIn: parent; width: 20; height: 20; radius: 10; color: parent.color; opacity: 0.22 }
                }
                Text { objectName: "jarvisDialogLabel"; anchors.verticalCenter: parent.verticalCenter
                       text: i18n.upper(dialog.label); color: dialog.tone; font: Theme.label }
            }
            Text { objectName: dialog.titleName; visible: text !== ""; width: parent.width
                   wrapMode: Text.Wrap; textFormat: Text.PlainText; text: dialog.title; color: Theme.text; font: Theme.subtitle }
        }

        Flickable {
            id: body
            objectName: "jarvisDialogBody"
            x: 22; y: header.y + header.height + 14; width: frame.width - 44
            height: Math.max(0, frame.height - y - footerArea.height)
            contentHeight: bodyColumn.height; clip: true
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { policy: body.contentHeight > body.height ? ScrollBar.AlwaysOn : ScrollBar.AsNeeded }
            Column { id: bodyColumn; width: body.width - (body.contentHeight > body.height ? 12 : 0); spacing: 10 }
        }

        Item {
            id: footerArea
            anchors.bottom: parent.bottom
            width: parent.width; height: footerRow.height + 36
            Rectangle { x: 22; width: parent.width - 44; height: 1; color: Theme.glassSeparator; visible: body.contentHeight > body.height }
            Row { id: footerRow; objectName: "jarvisDialogFooter"; anchors.right: parent.right; anchors.rightMargin: 22
                  anchors.verticalCenter: parent.verticalCenter; spacing: 10 }
        }
    }
}
