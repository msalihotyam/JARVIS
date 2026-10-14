import QtQuick
import "../theme"

// V2.2 S1: says which saved skill JARVIS is about to follow (D6). A pure view:
// output only through the signals; declining runs the task without the skill
// (D16: never a use, never a failure). Following a skill grants nothing - each
// step still meets the normal approval gate. A stale skill (S12) is never used
// on its own: the stale variant asks, `use` or `skip`.
Rectangle {
    id: chip
    property string skillName: ""
    property int version: 0
    property bool stale: false
    property string parentName: ""            // an adapted skill's parent, else ""
    property int parentVersion: 0
    signal declined()
    signal use()
    signal skip()

    implicitWidth: 560
    implicitHeight: body.height + 20
    radius: Theme.radiusSmall
    readonly property color tone: stale ? Theme.warning : Theme.accent
    color: Theme.alpha(tone, 0.08)
    border.width: 1; border.color: Theme.alpha(tone, 0.4)

    Column {
        id: body
        x: 12; y: 10; width: chip.width - 24; spacing: 8
        Row {
            width: parent.width; spacing: 8
            Icon { name: chip.stale ? "warning" : "spark"; size: 14; tint: chip.tone; anchors.verticalCenter: parent.verticalCenter }
            Text {
                objectName: "skillAnnounceText"
                width: parent.width - 22; wrapMode: Text.Wrap; textFormat: Text.PlainText
                color: Theme.text; font: Theme.caption; anchors.verticalCenter: parent.verticalCenter
                text: chip.stale ? qsTr("The \"%1\" skill failed in recent runs - should I use it? (v%2)").arg(chip.skillName).arg(chip.version)
                                 : qsTr("Using the \"%1\" skill for this (v%2)").arg(chip.skillName).arg(chip.version)
            }
        }
        Text { objectName: "skillAnnounceProvenance"; visible: chip.parentName !== ""; width: parent.width; x: 22
               wrapMode: Text.Wrap; textFormat: Text.PlainText; color: Theme.textLabel; font: Theme.caption
               text: qsTr("Adapted from the \"%1\" skill, v%2").arg(chip.parentName).arg(chip.parentVersion) }
        Row {
            x: 22; spacing: 8
            ActionButton { objectName: "skillAnnounceDecline"; visible: !chip.stale; compact: true; label: qsTr("Don't use"); onClicked: chip.declined() }
            ActionButton { objectName: "skillAnnounceUse"; visible: chip.stale; compact: true; label: qsTr("Use it"); onClicked: chip.use() }
            ActionButton { objectName: "skillAnnounceSkip"; visible: chip.stale; compact: true; label: qsTr("Skip"); onClicked: chip.skip() }
        }
    }
}
