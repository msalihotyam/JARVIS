import QtQuick
import "../theme"
import "../components"

Item {
    id: overlay
    anchors.fill: parent
    property bool fixture: false
    signal dismissFixture()
    readonly property bool pending: bridge.approvalPending || fixture
    visible: pending || dim.opacity > 0
    z: 450
    Rectangle {
        id: dim; anchors.fill: parent; color: "#000000"; opacity: overlay.pending ? 0.56 : 0
        Behavior on opacity { NumberAnimation { duration: Theme.approvalIn } }
        MouseArea { anchors.fill: parent; enabled: overlay.pending }
    }
    ApprovalCard {
        anchors.centerIn: parent
        width: Math.min(760, parent.width - 140); height: Math.min(818, parent.height - 154)
        fixture: overlay.fixture && !bridge.approvalPending
        onDismissFixture: overlay.dismissFixture()
        opacity: overlay.pending ? 1 : 0; scale: overlay.pending ? 1 : 0.975
        Behavior on opacity { NumberAnimation { duration: Theme.approvalIn; easing.type: Easing.OutCubic } }
        Behavior on scale { NumberAnimation { duration: Theme.approvalIn; easing.type: Easing.OutCubic } }
    }
}
