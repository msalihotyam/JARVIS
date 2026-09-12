import QtQuick
import QtQuick.Shapes
import QtQuick.Controls.Basic
import "../theme"
import "../components"

// The selector's open state, in the same Option 5 "Command" language as the
// closed trigger: premium dark glass, compact rows with the provider mark,
// the model name, the metadata the installation actually knows, and a
// restrained check on the active row.
//
// The rows are the choices that genuinely exist here: the CLI's own default
// and whatever model this installation is configured with. Claude Code
// takes a free-form model string, so anything else is entered directly -
// the panel never invents a catalogue, a context length or a price, and it
// still routes every change through the one authoritative applyModel call.
ContextPanel {
    id: modelPanel
    heading: "MODEL & PROVIDER"
    surfaceName: "modelSurface"
    panelWidth: 430; panelHeight: 476
    onOpenChanged: if (open) { modelField.text = bridge.config.claude_model || ""; effortPick.value = bridge.config.claude_effort || "" }
    QtObject { id: effortPick; property string value: "" }
    readonly property string current: bridge.config.claude_model || ""

    Column {
        width: parent.width; spacing: 14

        // -- the provider, as one instrument row ---------------------------
        Row { spacing: 13
            Rectangle {
                width: 38; height: 38; radius: 10
                color: Theme.surfaceDeep; border.width: 1; border.color: Theme.alpha(Theme.commandEdge, 0.34)
                Text { anchors.centerIn: parent; anchors.horizontalCenterOffset: -1; text: "A\\"; color: Theme.text
                       font { family: Theme.font; pixelSize: 19; weight: Font.DemiBold; letterSpacing: -1 } }
            }
            Column { spacing: 3
                Text { text: "Anthropic"; color: Theme.text; font { family: Theme.font; pixelSize: 17; weight: Font.DemiBold } }
                Text { text: "Claude Code CLI · subscription route"; color: Theme.textMuted; font { family: Theme.font; pixelSize: 12 } }
            }
        }
        Rectangle { width: parent.width; height: 1; color: Theme.border }

        Text { text: "MODEL"; color: Theme.textMuted; font { family: Theme.font; pixelSize: 10; letterSpacing: 2 } }

        // -- the real rows --------------------------------------------------
        Column {
            width: parent.width; spacing: 4
            Repeater {
                model: modelPanel.current ? [{ value: "", label: "CLI default", meta: "Whatever the Claude Code CLI selects" },
                                             { value: modelPanel.current, label: modelPanel.current, meta: "ANTHROPIC  |  CONFIGURED HERE" }]
                                          : [{ value: "", label: "CLI default", meta: "Whatever the Claude Code CLI selects" }]
                Rectangle {
                    width: parent.width; height: 46; radius: Theme.radiusSmall
                    readonly property bool selected: modelData.value === modelPanel.current
                    color: rowArea.containsMouse ? Theme.alpha(Theme.commandEdge, 0.08)
                                                 : selected ? Theme.alpha(Theme.commandEdge, 0.05) : "transparent"
                    border.width: 1
                    border.color: selected ? Theme.alpha(Theme.commandEdge, 0.42)
                                           : rowArea.containsMouse ? Theme.alpha(Theme.commandEdge, 0.22) : Theme.alpha(Theme.text, 0.05)
                    Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
                    Behavior on border.color { ColorAnimation { duration: Theme.hoverMs } }
                    Rectangle {
                        x: 8; anchors.verticalCenter: parent.verticalCenter
                        width: 28; height: 28; radius: 8
                        color: Theme.surfaceDeep; border.width: 1; border.color: Theme.alpha(Theme.commandEdge, 0.28)
                        Text { anchors.centerIn: parent; anchors.horizontalCenterOffset: -1; text: "A\\"; color: Theme.textLabel
                               font { family: Theme.font; pixelSize: 14; weight: Font.DemiBold; letterSpacing: -1 } }
                    }
                    Column {
                        x: 46; anchors.verticalCenter: parent.verticalCenter
                        width: parent.width - 46 - 34; spacing: 1
                        Text { width: parent.width; elide: Text.ElideRight; text: modelData.label
                               color: parent.parent.selected ? Theme.commandEdge : Theme.text
                               font { family: Theme.font; pixelSize: 15 } }
                        Text { width: parent.width; elide: Text.ElideRight; text: modelData.meta
                               color: Theme.textDim; font { family: Theme.font; pixelSize: 11; letterSpacing: 1.2 } }
                    }
                    // The selected-state marker: one drawn check, no badge.
                    Shape {
                        visible: parent.selected
                        anchors.verticalCenter: parent.verticalCenter
                        x: parent.width - 28; width: 14; height: 11
                        preferredRendererType: Shape.CurveRenderer
                        ShapePath { strokeColor: Theme.commandEdge; strokeWidth: 1.8; fillColor: "transparent"
                            capStyle: ShapePath.RoundCap; joinStyle: ShapePath.RoundJoin
                            startX: 0.9; startY: 5.5
                            PathLine { x: 5; y: 9.8 }
                            PathLine { x: 13.1; y: 1 }
                        }
                    }
                    MouseArea {
                        id: rowArea
                        objectName: modelData.value === "" ? "modelRowDefault" : "modelRowCurrent"
                        anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                        onClicked: bridge.applyModel(modelData.value, effortPick.value)
                    }
                }
            }
        }

        Text { text: "OTHER MODEL"; color: Theme.textMuted; font { family: Theme.font; pixelSize: 10; letterSpacing: 2 } }
        TextField {
            id: modelField; objectName: "modelField"
            width: parent.width; height: 38; leftPadding: 12; rightPadding: 12
            placeholderText: "CLI default"; placeholderTextColor: Theme.textDim; color: Theme.text
            selectByMouse: true; font { family: Theme.font; pixelSize: 14 }
            background: Rectangle { radius: 6; color: Theme.surfaceDeep; border.width: 1
                                    border.color: modelField.activeFocus ? Theme.commandEdge : Theme.border }
        }
        Text { text: "EFFORT"; color: Theme.textMuted; font { family: Theme.font; pixelSize: 10; letterSpacing: 2 } }
        Segmented { value: effortPick.value; onPicked: function(v) { effortPick.value = v }; options: [{value:"",label:"Default"}].concat(bridge.effortChoices.map(function(e) { return {value:e,label:e} })) }
        Row { spacing: 12
            ActionButton { objectName: "applyModelButton"; label: "Apply model / effort"; primary: true; onClicked: bridge.applyModel(modelField.text, effortPick.value) }
            Text { anchors.verticalCenter: parent.verticalCenter; text: "Validated configuration"; color: Theme.textDim; font { family: Theme.font; pixelSize: 11 } }
        }
    }
}
