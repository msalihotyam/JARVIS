import QtQuick
import "../theme"

// One quota window, calmly: its name, what REMAINS of it (the figure), when
// it resets (quieter, beside it) and a thin bar filled to exactly that
// fraction.
//
// Every number is the provider's own. A window a provider did not report is
// not drawn at all - there is no estimate, no average and no placeholder
// percentage anywhere in this component. ``exact`` false only dims the bar's
// fill; it never invents the value. A low remainder is attention (amber),
// never an error: nothing has failed.
Item {
    id: quota
    property string label: ""
    property real remaining: -1            // 0-100 as reported; below 0 means "not reported"
    property var resetsAt: 0               // epoch seconds; 0 means the provider named no reset
    property string resetText: ""
    property bool exact: true
    property color tone: Theme.accent

    readonly property bool reported: remaining >= 0
    readonly property bool low: reported && remaining <= 25
    width: parent ? parent.width : 0
    visible: reported
    height: visible ? head.height + 9 : 0

    Item {
        id: head
        width: parent.width; height: 18
        Text {
            objectName: "quotaLabel"
            text: quota.label; color: Theme.textLabel
            font: Theme.caption
            width: parent.width - figures.width - 12; elide: Text.ElideRight
            anchors.verticalCenter: parent.verticalCenter
        }
        Row {
            id: figures
            anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
            spacing: 6
            Text {
                objectName: "quotaValue"
                text: qsTr("%1% left").arg(Math.round(quota.remaining))
                color: quota.low ? Theme.warning : Theme.text
                font: Theme.captionStrong
            }
            Text {
                objectName: "quotaReset"
                visible: quota.resetText !== ""
                text: "·  " + quota.resetText
                color: Theme.textMuted
                font: Theme.caption
            }
        }
    }
    // The bar: one thin quiet track, one fill. No gradient animation, no pulse, no glow.
    Rectangle {
        y: head.height + 4
        width: parent.width; height: 3; radius: 1.5
        color: Theme.alpha(Theme.text, 0.06)
        Rectangle {
            objectName: "quotaFill"
            width: Math.max(0, Math.min(1, quota.remaining / 100)) * parent.width
            height: parent.height; radius: parent.radius
            color: quota.low ? Theme.warning : quota.tone
            opacity: quota.exact ? 0.75 : 0.4
            Behavior on width { NumberAnimation { duration: Theme.panelIn; easing.type: Easing.OutCubic } }
        }
    }
}
