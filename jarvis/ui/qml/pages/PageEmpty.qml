import QtQuick
import "../theme"
import "../components"

// K6: a page's honest empty state - what the place is, why it is empty, and
// one next step when there is one (no action label, no button).
Column {
    id: empty
    property string title: ""
    property string note: ""
    property string action: ""
    signal acted()
    spacing: 6
    Text { width: parent.width; wrapMode: Text.Wrap; text: empty.title; color: Theme.text; font: Theme.pageRowStrong }
    Text { width: parent.width; wrapMode: Text.Wrap; visible: text !== ""; text: empty.note; color: Theme.textMuted; font: Theme.pageMeta }
    Item { width: 1; height: 4; visible: empty.action !== "" }
    ActionButton { objectName: empty.objectName + "Action"; visible: empty.action !== ""; compact: true; label: empty.action; onClicked: empty.acted() }
}
