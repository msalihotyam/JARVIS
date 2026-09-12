import QtQuick
import QtQuick.Controls.Basic
import "../theme"
import "../components"

ContextPanel {
    id: modelPanel
    heading: "MODEL & PROVIDER"
    surfaceName: "modelSurface"
    panelWidth: 425; panelHeight: 385
    onOpenChanged: if (open) { modelField.text = bridge.config.claude_model || ""; effortPick.value = bridge.config.claude_effort || "" }
    QtObject { id: effortPick; property string value: "" }
    Column {
        width: parent.width; spacing: 17
        Row { spacing: 14
            IconBox { glyph: "AI"; tint: "#efb66a"; width: 42; height: 42; solid: true }
            Column { spacing: 3
                Text { text: "Claude"; color: Theme.text; font { family: Theme.font; pixelSize: 17; weight: Font.DemiBold } }
                Text { text: "Claude Code CLI · subscription route"; color: Theme.textMuted; font { family: Theme.font; pixelSize: 12 } }
            }
        }
        Rectangle { width: parent.width; height: 1; color: Theme.border }
        Text { text: "MODEL"; color: Theme.textMuted; font { family: Theme.font; pixelSize: 10; letterSpacing: 2 } }
        TextField {
            id: modelField; objectName: "modelField"
            width: parent.width; height: 38; leftPadding: 12; rightPadding: 12
            placeholderText: "CLI default"; placeholderTextColor: Theme.textDim; color: Theme.text
            selectByMouse: true; font { family: Theme.font; pixelSize: 14 }
            background: Rectangle { radius: 6; color: Theme.surfaceDeep; border.width: 1; border.color: modelField.activeFocus ? Theme.accent : Theme.border }
        }
        Text { text: "EFFORT"; color: Theme.textMuted; font { family: Theme.font; pixelSize: 10; letterSpacing: 2 } }
        Segmented { value: effortPick.value; onPicked: function(v) { effortPick.value = v }; options: [{value:"",label:"Default"}].concat(bridge.effortChoices.map(function(e) { return {value:e,label:e} })) }
        Row { spacing: 12
            ActionButton { objectName: "applyModelButton"; label: "Apply model / effort"; primary: true; onClicked: bridge.applyModel(modelField.text, effortPick.value) }
            Text { anchors.verticalCenter: parent.verticalCenter; text: "Validated configuration"; color: Theme.textDim; font { family: Theme.font; pixelSize: 11 } }
        }
    }
}
