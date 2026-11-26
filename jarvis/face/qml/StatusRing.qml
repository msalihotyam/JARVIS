import QtQuick
import QtQuick.Shapes

// FACERING1: the status ring of canvas frame 3. It only draws what it is given (state, level,
// count) and reads every colour, size and duration from face.tokens. Used at 72 px (desktop ring)
// and 28 px (header mark); below half the drawing box only the outer ring, main ring and core show.
Item {
    id: ring

    // -- ratios, angles and counts: not sizes, colours or durations --
    readonly property real turn: 360                  // degrees in a full turn
    readonly property real quarter: 90                // an arc spans about a quarter turn
    readonly property real swellMax: 1.07             // listening: main ring scale at full level
    readonly property real breathLow: 0.5             // breathing core: lowest opacity
    readonly property real halfCycle: 0.5             // speaking: the second ripple trails by half a cycle
    readonly property real dim: 0.6                   // main ring when it is not at full strength
    readonly property real quiet: 0.45                // outer and dashed rings
    readonly property real glowStrength: 0.16         // glow disc behind the core
    readonly property int badgeCap: 9                 // the badge shows "9+" above this
    // -- end --

    state: "idle"                                     // FACEPORT1 name; the component never sets it
    property alias mood: ring.state                   // FACESHELL1 name, still used by FaceWindow
    property real level: 0                            // voice level 0-1 (listening)
    property real count: 0                            // open asks (needs_you badge)
    property real size: 0                             // px; 0 = fit the item
    property string elapsed: ""                       // optional label beside the ring while thinking
    property bool reducedMotion: false
    signal clicked()
    signal unknownState(string name)

    readonly property var moods: ["idle", "listening", "understanding", "thinking", "speaking", "needs_you"]
    readonly property var labels: ({
        idle: qsTr("Boşta"), listening: qsTr("Dinliyor"), understanding: qsTr("Anlıyor"),
        thinking: qsTr("Düşünüyor"), speaking: qsTr("Konuşuyor"), needs_you: qsTr("Senden bir şey istiyor")
    })
    readonly property string shown: moods.indexOf(state) >= 0 ? state : "idle"
    readonly property string label: labels[shown]
    readonly property real lv: isNaN(level) ? 0 : Math.max(0, Math.min(1, level))
    readonly property int asks: isNaN(count) ? 0 : Math.max(0, Math.floor(Math.min(count, badgeCap + 1)))
    readonly property string badgeText: asks > badgeCap ? badgeCap + "+" : String(asks)
    readonly property string accessibleName: shown === "needs_you"
                                             ? qsTr("%1 (%2)").arg(label).arg(badgeText) : label
    onStateChanged: if (moods.indexOf(state) < 0) unknownState(state)

    // The one way in: face.tokens (flat keys, as FACETOKENS1 as_qml_dict). Missing -> 0 / transparent.
    readonly property var tk: typeof face !== "undefined" && face ? face.tokens : ({})
    function t(name) { var v = tk ? tk[name] : undefined; return typeof v === "number" && isFinite(v) ? v : 0 }
    function c(name) { var v = tk ? tk[name] : undefined; return typeof v === "string" && v !== "" ? v : "transparent" }

    readonly property real box: t("ring_box")
    readonly property real side: size > 0 ? size : Math.min(width, height)
    readonly property real k: box > 0 ? side / box : 0
    readonly property bool compact: side * 2 < box    // under half the drawing box: the header mark
    readonly property bool animate: !reducedMotion
    readonly property color stroke: c(shown === "needs_you" ? "color_needs_you" : "color_accent")
    readonly property bool breathing: shown === "idle" || shown === "thinking"
    readonly property bool elapsedOn: shown === "thinking" && !compact && elapsed !== ""
    readonly property real mainScale: shown === "listening" ? 1 + (swellMax - 1) * lv : 1
    readonly property real mainStrength: shown === "listening" ? 1
                                       : shown === "needs_you" ? dim + (1 - dim) * (animate ? pulse : 1) : dim
    readonly property real ripplePhase: animate ? ripple : 0
    readonly property bool moving: animate && visible        // hidden rings do not animate
    function u(name) { return t(name) * k }                  // drawing-box units -> px
    function sw(name) { return Math.max(1, u(name)) }        // stroke width, never under one pixel
    function rippleWidth(p) { return 2 * (u("ring_core_r") + (u("ring_main_r") - u("ring_core_r")) * p) + sw("ring_outer_w") }

    // Visual phases, written only by the animations at the bottom.
    property real breath: 1
    property real pulse: 1
    property real ripple: 0

    implicitWidth: size + (size > 0 && elapsedOn ? t("space_8") + elapsedLabel.implicitWidth : 0)
    implicitHeight: size

    Item {
        id: canvas
        width: ring.side; height: ring.side
        x: ring.elapsedOn ? 0 : (ring.width - width) / 2
        y: (ring.height - height) / 2

        Rectangle {
            objectName: "outerRing"
            readonly property real line: ring.sw("ring_outer_w")
            anchors.centerIn: parent
            width: 2 * ring.u("ring_outer_r") + line; height: width; radius: width / 2
            color: "transparent"; border.width: line; border.color: ring.stroke
            opacity: ring.quiet
        }
        Shape {
            objectName: "dashRing"
            visible: !ring.compact && ring.box > 0
            anchors.fill: parent
            opacity: ring.quiet
            ShapePath {
                strokeColor: ring.stroke; fillColor: "transparent"
                strokeWidth: ring.sw("ring_dash_w")
                strokeStyle: ring.box > 0 ? ShapePath.DashLine : ShapePath.SolidLine
                dashPattern: [ring.u("ring_dash_on") / strokeWidth, ring.u("ring_dash_off") / strokeWidth]
                PathAngleArc {
                    centerX: ring.side / 2; centerY: ring.side / 2
                    radiusX: ring.u("ring_dash_r"); radiusY: radiusX
                    startAngle: 0; sweepAngle: ring.turn
                }
            }
        }
        Rectangle {
            objectName: "mainRing"
            readonly property real line: ring.sw("ring_main_w")
            anchors.centerIn: parent
            width: 2 * ring.u("ring_main_r") + line; height: width; radius: width / 2
            color: "transparent"; border.width: line; border.color: ring.stroke
            opacity: ring.mainStrength
            // Disabling a Behavior does not stop its running animation, so the swell
            // animates a side value the ring drops the moment it stops moving; reading
            // swell.enabled re-writes swollen past the now-disabled Behavior, which stops it.
            property real swollen: (swell.enabled, ring.mainScale)
            scale: ring.moving ? swollen : ring.mainScale
            Behavior on swollen {
                id: swell
                enabled: ring.moving && ring.t("motion_swell") > 0
                NumberAnimation {
                    duration: ring.t("motion_swell"); easing.type: Easing.Bezier
                    easing.bezierCurve: [ring.t("motion_curve_x1"), ring.t("motion_curve_y1"),
                                         ring.t("motion_curve_x2"), ring.t("motion_curve_y2"), 1, 1]
                }
            }
        }
        Rectangle {
            objectName: "glow"
            visible: !ring.compact && ring.shown !== "idle"
            anchors.centerIn: parent
            width: 2 * ring.u("ring_glow_r"); height: width; radius: width / 2
            color: ring.stroke; opacity: ring.glowStrength
        }
        Rectangle {
            objectName: "ripple1"
            readonly property real p: ring.ripplePhase
            visible: ring.shown === "speaking" && !ring.compact
            anchors.centerIn: parent
            width: ring.rippleWidth(p); height: width; radius: width / 2
            color: "transparent"; border.width: ring.sw("ring_outer_w"); border.color: ring.stroke
            opacity: 1 - p
        }
        Rectangle {
            objectName: "ripple2"
            readonly property real p: (ring.ripplePhase + ring.halfCycle) % 1
            visible: ring.shown === "speaking" && !ring.compact
            anchors.centerIn: parent
            width: ring.rippleWidth(p); height: width; radius: width / 2
            color: "transparent"; border.width: ring.sw("ring_outer_w"); border.color: ring.stroke
            opacity: 1 - p
        }
        Item {
            id: arcUnderstand
            objectName: "arcUnderstand"
            visible: ring.shown === "understanding" && !ring.compact
            anchors.fill: parent
            Shape {
                anchors.fill: parent
                ShapePath {
                    strokeColor: ring.stroke; fillColor: "transparent"
                    strokeWidth: ring.sw("ring_main_w"); capStyle: ShapePath.RoundCap
                    PathAngleArc {
                        centerX: ring.side / 2; centerY: ring.side / 2
                        radiusX: ring.u("ring_main_r"); radiusY: radiusX
                        startAngle: -ring.quarter; sweepAngle: ring.quarter
                    }
                }
            }
            RotationAnimator on rotation {
                running: arcUnderstand.visible && ring.moving && ring.t("motion_arc") > 0
                from: 0; to: ring.turn; duration: ring.t("motion_arc"); loops: Animation.Infinite
            }
        }
        Item {
            id: arcThinkA
            objectName: "arcThinkA"
            visible: ring.shown === "thinking" && !ring.compact
            anchors.fill: parent
            Shape {
                anchors.fill: parent
                ShapePath {
                    strokeColor: ring.stroke; fillColor: "transparent"
                    strokeWidth: ring.sw("ring_main_w"); capStyle: ShapePath.RoundCap
                    PathAngleArc {
                        centerX: ring.side / 2; centerY: ring.side / 2
                        radiusX: ring.u("ring_main_r"); radiusY: radiusX
                        startAngle: -ring.quarter; sweepAngle: ring.quarter
                    }
                }
            }
            RotationAnimator on rotation {
                running: arcThinkA.visible && ring.moving && ring.t("motion_arc2") > 0
                from: 0; to: ring.turn; duration: ring.t("motion_arc2"); loops: Animation.Infinite
            }
        }
        Item {
            id: arcThinkB
            objectName: "arcThinkB"
            visible: ring.shown === "thinking" && !ring.compact
            anchors.fill: parent
            Shape {
                anchors.fill: parent
                ShapePath {
                    strokeColor: ring.stroke; fillColor: "transparent"
                    strokeWidth: ring.sw("ring_dash_w"); capStyle: ShapePath.RoundCap
                    PathAngleArc {
                        centerX: ring.side / 2; centerY: ring.side / 2
                        radiusX: ring.u("ring_dash_r"); radiusY: radiusX
                        startAngle: ring.quarter; sweepAngle: ring.quarter
                    }
                }
            }
            RotationAnimator on rotation {           // the opposite way to arcThinkA
                running: arcThinkB.visible && ring.moving && ring.t("motion_arc3") > 0
                from: ring.turn; to: 0; duration: ring.t("motion_arc3"); loops: Animation.Infinite
            }
        }
        Rectangle {
            objectName: "core"
            anchors.centerIn: parent
            width: 2 * ring.u("ring_core_r"); height: width; radius: width / 2
            color: ring.stroke
            opacity: ring.breathing && ring.animate ? ring.breath : 1
        }
        Rectangle {
            objectName: "badge"
            visible: ring.shown === "needs_you" && ring.asks > 0 && !ring.compact
            height: ring.t("space_20"); width: Math.max(height, digits.implicitWidth + 2 * ring.t("space_4"))
            x: parent.width - width; y: 0
            radius: ring.t("radius_pill")
            color: ring.c("color_needs_you")
            Text {
                id: digits
                objectName: "badgeText"
                anchors.centerIn: parent
                textFormat: Text.PlainText
                text: ring.badgeText
                color: ring.c("color_needs_you_ink")
                font.pixelSize: Math.max(1, ring.t("type_label_size")); font.bold: true
            }
        }
    }

    Text {
        id: elapsedLabel
        objectName: "elapsedLabel"
        visible: ring.elapsedOn
        textFormat: Text.PlainText
        text: ring.elapsed
        color: ring.c("color_text2")
        font.pixelSize: Math.max(1, ring.t("type_meta_size"))
        x: canvas.x + canvas.width + ring.t("space_8")
        anchors.verticalCenter: canvas.verticalCenter
    }

    MouseArea {
        objectName: "ringHit"
        readonly property real minHit: ring.t("space_24") + ring.t("space_20")   // 44 px touch target
        anchors.centerIn: canvas
        width: Math.max(canvas.width, minHit); height: Math.max(canvas.height, minHit)
        cursorShape: Qt.PointingHandCursor
        Accessible.role: Accessible.Button
        Accessible.name: ring.accessibleName
        Accessible.onPressAction: ring.clicked()
        onClicked: ring.clicked()
    }

    // Visual phases only; each stops in reduced motion, when hidden, or when its duration is missing.
    NumberAnimation on breath {
        objectName: "breathAnim"
        running: ring.moving && ring.breathing && ring.t("motion_breathe") > 0
        from: ring.breathLow; to: 1; duration: ring.t("motion_breathe")
        easing.type: Easing.SineCurve; loops: Animation.Infinite        // low -> 1 -> low in one cycle
    }
    NumberAnimation on pulse {
        objectName: "pulseAnim"
        running: ring.moving && ring.shown === "needs_you" && ring.t("motion_ask") > 0
        from: 0; to: 1; duration: ring.t("motion_ask")
        easing.type: Easing.SineCurve; loops: Animation.Infinite
    }
    NumberAnimation on ripple {
        objectName: "rippleAnim"
        running: ring.moving && ring.shown === "speaking" && !ring.compact && ring.t("motion_ripple") > 0
        from: 0; to: 1; duration: ring.t("motion_ripple"); loops: Animation.Infinite
    }
}
