import QtQuick
import QtQuick.Controls.Basic
import "../theme"

// PM29 project run card: a read-only mirror of bridge.projectCard. Every
// text comes from the bridge (plain text); every button only calls a bridge
// slot, is enabled from the bridge's flags, and Core re-checks the action.
Item {
    id: root
    objectName: "projectRunCard"
    property real inset: 0
    readonly property var card: bridge.projectCard
    readonly property var labels: card.labels || ({})
    readonly property real cardWidth: Math.min(760, width - inset)
    implicitHeight: surface.height + 12

    Rectangle {
        id: surface
        x: root.inset
        width: root.cardWidth
        height: body.implicitHeight + 24
        radius: 12
        color: Theme.glassPrimaryReading
        border.color: Theme.alpha(Theme.text, 0.25)

        Column {
            id: body
            x: 14; y: 12
            width: parent.width - 28
            spacing: 6

            Text { width: parent.width; textFormat: Text.PlainText; wrapMode: Text.Wrap; color: Theme.text
                   font: Theme.uiLabel; renderType: Theme.textRender
                   text: (root.labels.title || "") + " — " + (root.card.stateLabel || "") }
            Repeater {
                model: [["slice", "currentSlice"], ["checks", "checks"], ["diff", "diffSummary"], ["hint", "runHint"]]
                Text { required property var modelData
                       visible: !!root.card[modelData[1]]; width: body.width; textFormat: Text.PlainText
                       wrapMode: Text.Wrap; color: Theme.textMuted; font: Theme.metadata; renderType: Theme.textRender
                       text: (root.labels[modelData[0]] || "") + ": " + (root.card[modelData[1]] || "") }
            }
            Text { visible: !!root.card.notice; width: parent.width; textFormat: Text.PlainText; wrapMode: Text.Wrap
                   color: Theme.text; font: Theme.metadata; renderType: Theme.textRender; text: root.card.notice || "" }
            Flow {
                width: parent.width
                spacing: 8
                ActionButton { objectName: "projectApprove"; compact: true; primary: true; visible: enabled
                               enabled: !!root.card.canApprove; label: root.labels.approve || ""
                               onClicked: bridge.approveProjectPlan() }
                ActionButton { objectName: "projectReject"; compact: true; danger: true; visible: enabled
                               enabled: !!root.card.canReject; label: root.labels.reject || ""
                               onClicked: bridge.rejectProjectPlan() }
                ActionButton { objectName: "projectPause"; compact: true; enabled: !!root.card.canPause
                               label: root.labels.pause || ""; onClicked: bridge.pauseProjectRun() }
                ActionButton { objectName: "projectCancel"; compact: true; danger: true; enabled: !!root.card.canCancel
                               label: root.labels.cancel || ""; onClicked: bridge.cancelProjectRun() }
                ActionButton { objectName: "projectShow"; compact: true; visible: enabled
                               enabled: !!root.card.canShowResult; label: root.labels.show || ""
                               onClicked: bridge.showProjectResult() }
            }
            Text { visible: (root.card.notifications || []).length > 0; textFormat: Text.PlainText; color: Theme.textMuted
                   font: Theme.metadata; renderType: Theme.textRender; text: root.labels.recent || "" }
            Repeater {
                model: root.card.notifications || []
                Text { required property var modelData
                       width: body.width; textFormat: Text.PlainText; elide: Text.ElideRight; color: Theme.textMuted
                       font: Theme.metadata; renderType: Theme.textRender; text: modelData }
            }
        }
    }
}
