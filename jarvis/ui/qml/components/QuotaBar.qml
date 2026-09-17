import QtQuick
import "../theme"

// One quota window, calmly: its name, what REMAINS of it, when it resets and
// a bar filled to exactly that fraction.
//
// Every number is the provider's own. A window a provider did not report is
// not drawn at all - there is no estimate, no average and no placeholder
// percentage anywhere in this component. ``exact`` false only dims the bar's
// fill; it never invents the value.
Item {
    id: quota
    property string label: ""
    property real remaining: -1            // 0-100 as reported; below 0 means "not reported"
    property var resetsAt: 0               // epoch seconds; 0 means the provider named no reset
    property string resetText: ""
    property bool exact: true
    property color tone: Theme.accent

    readonly property bool reported: remaining >= 0
    width: parent ? parent.width : 0
    visible: reported
    height: visible ? head.height + 10 : 0

    Item {
        id: head
        width: parent.width; height: 16
        Text {
            objectName: "quotaLabel"
            text: quota.label; color: Theme.textLabel
            font: Qt.font({ family: Theme.font, pixelSize: 12 })
            width: parent.width - value.width - 12; elide: Text.ElideRight
            anchors.verticalCenter: parent.verticalCenter
        }
        Text {
            id: value
            objectName: "quotaValue"
            anchors.right: parent.right; anchors.verticalCenter: parent.verticalCenter
            text: quota.resetText ? qsTr("%1% left · %2").arg(Math.round(quota.remaining)).arg(quota.resetText)
                                  : qsTr("%1% left").arg(Math.round(quota.remaining))
            color: Theme.text
            font: Qt.font({ family: Theme.font, pixelSize: 12, weight: Font.DemiBold })
        }
    }
    // The bar: one calm track, one fill. No gradient animation, no pulse.
    Rectangle {
        y: head.height + 5
        width: parent.width; height: 5; radius: 2.5
        color: Theme.alpha(Theme.text, 0.07)
        Rectangle {
            objectName: "quotaFill"
            width: Math.max(0, Math.min(1, quota.remaining / 100)) * parent.width
            height: parent.height; radius: parent.radius
            color: quota.remaining <= 10 ? Theme.danger : quota.remaining <= 25 ? Theme.warning : quota.tone
            opacity: quota.exact ? 0.85 : 0.45
            Behavior on width { NumberAnimation { duration: Theme.panelIn; easing.type: Easing.OutCubic } }
        }
    }
}
