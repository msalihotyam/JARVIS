import QtQuick
import QtQuick.Shapes
import QtQuick.Controls.Basic
import "../theme"

// The canonical command header. Left: the identity mark (which is also the
// Settings trigger), the wordmark, and the ambient rail (date / time / local
// context) whose geometry comes from the Phase 1 macro map. Centre: the
// JARVIS core, the dominant element, with its lateral connector lines and
// the core-side status group. Right: the Option 5 command strip -
// selector, status instrument, Freeze control, navigation grid, window
// controls - anchored to the right edge and bound to real state only.
Item {
    id: bar
    // Height, rail width and the viewport height come from the macro
    // layout (Main.qml); the rail's own rhythm is proportional to it.
    property real railWidth: 246
    property real viewportHeight: 1080
    // The rail's own left inset, proportional like the rail itself.
    readonly property real railInset: Theme.px(bar.width * 0.0163, 20, 34)
    signal menuRequested()
    signal lifecycleRequested()      // the red control: Freeze / Resume JARVIS
    signal editRequested()
    signal minimizeRequested()
    signal restoreRequested()
    signal closeRequested()
    signal openSettings()
    signal openModel()
    property date now: new Date()
    property bool fullscreen: true
    property bool settingsOpen: false
    property bool modelOpen: false
    property Item controlLayer: null

    // -- the scope group's parts (Gate 6E) --------------------------------------
    // One row of the scope menus: menuRowHeight; a hover lift; the current row
    // carries the 2 px accent mark; an action row reads in accent, an enabled
    // destructive row in danger, an untitled one in textLabel. An elided label
    // has its tip.
    component ScopeChoice: Rectangle {
        id: choice
        property string label: ""
        property bool current: false
        property bool available: true
        property bool action: false
        property bool danger: false
        property bool muted: false
        signal chosen()
        height: Theme.menuRowHeight; radius: 6
        enabled: !bridge.busy && available
        opacity: enabled ? 1 : 0.45
        color: current ? Theme.alpha(Theme.accent, 0.14) : choiceArea.containsMouse ? Theme.alpha(Theme.text, 0.07) : "transparent"
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
        Rectangle { visible: choice.current; x: 0; y: 8; width: 2; height: parent.height - 16; radius: 1; color: Theme.accent }
        Text { id: choiceText; x: 12; width: parent.width - 24; anchors.verticalCenter: parent.verticalCenter; text: choice.label
               textFormat: Text.PlainText; elide: Text.ElideRight; font: Theme.body
               color: choice.danger && choice.enabled ? Theme.danger : choice.action ? Theme.accent
                      : choice.muted ? Theme.textLabel : Theme.text }
        MouseArea { id: choiceArea; anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
                    onClicked: choice.chosen() }
        HoverTip { text: choice.label; active: choiceText.truncated }
    }
    // A disabled item's reason, under it; and a menu's section divider.
    component Reason: Text {
        leftPadding: 12; rightPadding: 12; bottomPadding: 4
        wrapMode: Text.Wrap; textFormat: Text.PlainText; color: Theme.textDim; font: Theme.caption
    }
    component Divider: Item {
        height: 9
        Rectangle { y: 4; x: 8; width: parent.width - 16; height: 1; color: Theme.border }
    }
    // A drawn chevron (no font glyph): it turns over while its menu is open.
    component Chevron: Shape {
        id: chevron
        property bool open: false
        width: 10; height: 6
        rotation: open ? 180 : 0
        Behavior on rotation { enabled: Theme.ambient; NumberAnimation { duration: Theme.panelIn; easing.type: Theme.ease } }
        preferredRendererType: Shape.CurveRenderer
        ShapePath {
            strokeColor: Theme.textLabel; strokeWidth: 1.5; fillColor: "transparent"
            capStyle: ShapePath.RoundCap; joinStyle: ShapePath.RoundJoin
            startX: 1; startY: 1
            PathLine { x: 5; y: 5 }
            PathLine { x: 9; y: 1 }
        }
    }
    // A scope pill: pillHeight, hover fill over hoverMs, press .97, the focus
    // ring. Click / Return / Space (and Down on a menu pill) activate it; a
    // right click, Menu or Shift+F10 ask for its context menu.
    component ScopePill: Rectangle {
        id: pill
        property color idleFill: Theme.alpha(Theme.text, 0.04)
        property color hotFill: Theme.alpha(Theme.text, 0.10)
        property bool open: false
        property bool menuKeys: true
        readonly property bool hovered: pillArea.containsMouse
        signal activated()
        signal contextRequested()
        signal dismissRequested()
        height: Theme.pillHeight; radius: height / 2
        color: hovered || open ? hotFill : idleFill
        Behavior on color { ColorAnimation { duration: Theme.hoverMs } }
        border.width: 1
        scale: pillArea.pressed ? 0.97 : 1
        Behavior on scale { enabled: Theme.ambient; NumberAnimation { duration: 90 } }
        activeFocusOnTab: true
        // Escape closes this pill's own open menu (the menus do not take focus),
        // before the window's Escape chain sees it.
        Keys.onShortcutOverride: function (event) { if (event.key === Qt.Key_Escape && pill.open) event.accepted = true }
        Keys.onPressed: function (event) {
            var key = event.key
            if (key === Qt.Key_Escape && pill.open) {
                event.accepted = true; pill.dismissRequested()
            } else if (key === Qt.Key_Return || key === Qt.Key_Enter || key === Qt.Key_Space || (pill.menuKeys && key === Qt.Key_Down)) {
                event.accepted = true; pill.activated()
            } else if (key === Qt.Key_Menu || (key === Qt.Key_F10 && (event.modifiers & Qt.ShiftModifier))) {
                event.accepted = true; pill.contextRequested()
            }
        }
        Rectangle { objectName: "scopePillFocusRing"; visible: pill.activeFocus; anchors.fill: parent; anchors.margins: -3
                    radius: pill.radius + 3; color: "transparent"; border.width: 2; border.color: Theme.alpha(Theme.materialGlint, 0.95) }
        MouseArea {
            id: pillArea
            anchors.fill: parent; hoverEnabled: true; cursorShape: Qt.PointingHandCursor
            acceptedButtons: Qt.LeftButton | Qt.RightButton
            onClicked: function (mouse) { if (mouse.button === Qt.RightButton) pill.contextRequested(); else pill.activated() }
        }
    }
    // The scope menus: glass, and the popover motion - opacity and panelTravel
    // over panelIn in, panelOut out; no motion at all with ambient effects off.
    component ScopeMenu: Popup {
        id: menu
        property real restY: 0
        y: restY
        padding: 8
        background: Rectangle { radius: 10; color: Theme.glassOverlay; border.width: 1; border.color: Theme.glassOverlayEdge
                                WheelHandler { } }
        enter: Theme.ambient ? menuIn : null
        exit: Theme.ambient ? menuOut : null
        readonly property Transition menuIn: Transition {
            ParallelAnimation {
                NumberAnimation { property: "opacity"; from: 0; to: 1; duration: Theme.panelIn; easing.type: Theme.ease }
                NumberAnimation { property: "y"; from: menu.restY - Theme.panelTravel; to: menu.restY; duration: Theme.panelIn; easing.type: Theme.ease }
            }
        }
        readonly property Transition menuOut: Transition {
            NumberAnimation { property: "opacity"; to: 0; duration: Theme.panelOut; easing.type: Theme.ease }
        }
        onClosed: { opacity = 1; y = restY }
    }
    readonly property point settingsAnchor: Qt.point(orb.x - 4, orb.y + orb.height + 14)
    readonly property point modelAnchor: Qt.point(controls.x + modelChip.x, controls.y + modelChip.height + 16)
    readonly property point killAnchor: Qt.point(controls.x + killSlot.x + killSlot.width, controls.y + controls.height + 22)
    readonly property point navAnchor:Qt.point(controls.x + navigationTrigger.x + navigationTrigger.width, controls.y + navigationTrigger.height + 18)
    // Degradation, in the required order: decorative linework goes first,
    // then secondary metadata, then micro spacing - never hit targets and
    // never the authoritative controls.
    readonly property bool narrow: width < 1360         // decorative linework
    readonly property bool compact: width < 1420        // metadata / spacing
    // The core keeps the reference's centre and a proportional diameter at
    // every width: it is the primary identity anchor, never repositioned
    // out of the composition.
    readonly property real coreCx: Theme.command ? width * 0.42 : width * Theme.macroCoreX
    readonly property real coreCy: Theme.command ? 60 : bar.viewportHeight * Theme.macroCoreY
    readonly property real coreDiameter: Theme.command ? 162 : Theme.coreSize(width)
    readonly property real coreEdge: coreCx + coreDiameter / 2
    // The strip is anchored right and the selector takes what is left, but
    // the core-side status group is reserved space first: the selector is
    // only trimmed while it stays at a usable width, and below that the
    // group (secondary metadata) is what gives way - never the controls.
    readonly property real stripGap: compact ? 8 : 12
    readonly property real tickerNeed: 88
    readonly property real ticker0: coreEdge + 20
    readonly property real stripFixed: Theme.stripStatus + Theme.stripKill + 38 + 13 + 106 + 5 * stripGap
    readonly property real stripRoom: width - Math.round(width * Theme.macroStripRight)
                                      - (ticker0 + tickerNeed + 10)
    readonly property real selectorMax: stripRoom - stripFixed
    readonly property bool tickerFits: !Theme.command && selectorMax >= 188
    // The card carries the plan summary on its right when a provider reported
    // one, so it is allowed that much more width - never at the cost of the
    // controls, and never past the room the strip really has.
    readonly property real selectorExtra: ((bridge.modelCard || {}).quota || []).length > 0 ? 150 : 0
    readonly property real selectorWidth: tickerFits ? Math.min(Theme.chipWidth(width) + selectorExtra, selectorMax)
                                                     : Theme.chipWidth(width)

    // -- header linework ------------------------------------------------------
    // The reference runs one fine cyan line across the header, broken around
    // the core, with an angled return on the right and a second line closing
    // the identity zone at the boundary the panels start from.
    Shape {
        id: hudLines
        anchors.fill: parent
        opacity: Theme.hudOpacity * (bar.narrow ? 0.8 : 1)
        preferredRendererType: Shape.CurveRenderer
        readonly property real yLine: Theme.command ? 100 : Theme.hudLine(bar.viewportHeight)
        readonly property real yRight: Theme.command ? 90 : Math.round(hudLines.yLine * 0.84)
        ShapePath {
            strokeColor: Theme.hud; strokeWidth: 1; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            startX: bar.width * 0.245; startY: hudLines.yLine + 22
            PathLine { x: bar.width * 0.265; y: hudLines.yLine }
            PathLine { x: bar.coreCx - bar.coreDiameter * 0.56; y: hudLines.yLine }
        }
        ShapePath {
            strokeColor: Theme.hud; strokeWidth: 1; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            startX: bar.coreCx + bar.coreDiameter * 0.56; startY: hudLines.yLine
            PathLine { x: bar.width * 0.655; y: hudLines.yLine }
            PathLine { x: bar.width * 0.685; y: hudLines.yRight }
            PathLine { x: bar.width * 0.93; y: hudLines.yRight }
        }
        ShapePath {
            strokeColor: Theme.alpha(Theme.hud, 0.45); strokeWidth: 1; fillColor: "transparent"
            startX: bar.width * 0.31; startY: hudLines.yLine + 7
            PathLine { x: bar.coreCx - bar.coreDiameter * 0.62; y: hudLines.yLine + 7 }
        }
        ShapePath {
            strokeColor: Theme.alpha(Theme.hud, 0.45); strokeWidth: 1; fillColor: "transparent"
            startX: bar.coreCx + bar.coreDiameter * 0.82; startY: hudLines.yLine + 7
            PathLine { x: bar.width * 0.62; y: hudLines.yLine + 7 }
        }
    }
    Repeater {
        model: 4
        Rectangle { visible: !bar.narrow; x: bar.width * (0.74 + index * 0.05); y: hudLines.yRight - 4; width: 1; height: 9; color: Theme.hud; opacity: Theme.hudOpacity * 1.2 }
    }
    Rectangle { visible: !bar.narrow; x: bar.width * 0.245 - 2.5; y: hudLines.yLine + 22 - 2.5; width: 5; height: 5; radius: 2.5; color: Theme.hud; opacity: Theme.hudOpacity * 1.6
        Rectangle { anchors.centerIn: parent; width: 13; height: 13; radius: 6.5; color: Theme.hud; opacity: 0.25 } }
    Rectangle { visible: !bar.narrow; x: bar.width * 0.93 - 2.5; y: hudLines.yRight - 2.5; width: 5; height: 5; radius: 2.5; color: Theme.hud; opacity: Theme.hudOpacity * 1.6
        Rectangle { anchors.centerIn: parent; width: 13; height: 13; radius: 6.5; color: Theme.hud; opacity: 0.25 } }
    // The boundary the panels start from: one restrained full-width line.
    Rectangle {
        visible: !Theme.command
        x: 0; y: bar.height - 1; width: bar.width; height: 1
        color: Theme.hud; opacity: Theme.hudOpacity * 0.7
    }

    // Lateral connector traces at the core's own height: the reference ties
    // the core into the header with fine lines, a dash and one node.
    Shape {
        id: coreTrace
        anchors.fill: parent
        visible: !bar.narrow && !Theme.command
        opacity: Theme.hudOpacity
        preferredRendererType: Shape.CurveRenderer
        ShapePath {
            strokeColor: Theme.hud; strokeWidth: 1; fillColor: "transparent"; capStyle: ShapePath.FlatCap
            startX: bar.coreCx - bar.coreDiameter * 0.52 - 116; startY: bar.coreCy
            PathLine { x: bar.coreCx - bar.coreDiameter * 0.52; y: bar.coreCy }
        }
        ShapePath {
            strokeColor: Theme.alpha(Theme.hud, 0.5); strokeWidth: 1; fillColor: "transparent"
            strokeStyle: ShapePath.DashLine; dashPattern: [2, 5]
            startX: bar.coreCx - bar.coreDiameter * 0.52 - 182; startY: bar.coreCy
            PathLine { x: bar.coreCx - bar.coreDiameter * 0.52 - 124; y: bar.coreCy }
        }
        ShapePath {
            strokeColor: Theme.hud; strokeWidth: 1; fillColor: "transparent"; capStyle: ShapePath.FlatCap
            startX: bar.coreCx + bar.coreDiameter * 0.51; startY: bar.coreCy
            PathLine { x: bar.ticker0 - 7; y: bar.coreCy }
        }
    }
    Rectangle {
        visible: coreTrace.visible
        x: bar.coreCx - bar.coreDiameter * 0.52 - 118; y: bar.coreCy - 2.5
        width: 5; height: 5; radius: 2.5; color: Theme.hud; opacity: Theme.hudOpacity * 1.6
        Rectangle { anchors.centerIn: parent; width: 13; height: 13; radius: 6.5; color: Theme.hud; opacity: 0.25 }
    }

    // Trajectory linework: a wide, shallow ellipse whose centre sits left
    // of the core, with a dashed companion inside it - the reference's
    // flattened trajectory, not a circle around the core. Measured from the
    // canonical reference: centre 0.292 core-diameters left of the core,
    // rx 0.699 d, ry 0.123 d (5.7:1). Its right end runs in behind the ring
    // family, which draws over it.
    Shape {
        id: orbit
        visible: !bar.narrow
        anchors.fill: parent
        opacity: Theme.hudOpacity * 0.8
        preferredRendererType: Shape.CurveRenderer
        readonly property real cx: bar.coreCx - bar.coreDiameter * 0.292
        readonly property real cy: bar.coreCy
        readonly property real rx: bar.coreDiameter * 0.699
        readonly property real ry: bar.coreDiameter * 0.123
        ShapePath {
            strokeColor: Theme.hud; strokeWidth: 1; fillColor: "transparent"; capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: orbit.cx; centerY: orbit.cy
                           radiusX: orbit.rx; radiusY: orbit.ry
                           startAngle: 78; sweepAngle: 204 }
        }
        ShapePath {
            strokeColor: Theme.alpha(Theme.hud, 0.6); strokeWidth: 1; fillColor: "transparent"
            strokeStyle: ShapePath.DashLine; dashPattern: [1, 5]
            PathAngleArc { centerX: orbit.cx; centerY: orbit.cy
                           radiusX: bar.coreDiameter * 0.627; radiusY: bar.coreDiameter * 0.088
                           startAngle: 92; sweepAngle: 176 }
        }
    }
    // One tracking node, on the ellipse at its leftmost vertex.
    Rectangle {
        visible: !bar.narrow
        width: 5; height: 5; radius: 2.5; color: Theme.hud; opacity: Theme.hudOpacity * 1.6
        x: orbit.cx + orbit.rx * Math.cos(175 * Math.PI / 180) - 2.5
        y: orbit.cy + orbit.ry * Math.sin(175 * Math.PI / 180) - 2.5
        Rectangle { anchors.centerIn: parent; width: 13; height: 13; radius: 6.5; color: Theme.hud; opacity: 0.25 }
    }

    // -- brand: the identity mark IS the Settings trigger ---------------------
    Item {
        id: orb
        parent: bar.controlLayer || bar
        objectName: "settingsOrb"
        x: 28; y: 16; width: 62; height: 62
        readonly property bool hovered: orbArea.containsMouse
        readonly property bool lit: hovered || bar.settingsOpen
        activeFocusOnTab: true
        Accessible.role: Accessible.Button; Accessible.name: qsTr("General Settings")
        Keys.onReturnPressed: bar.openSettings()
        Keys.onSpacePressed: bar.openSettings()
        Rectangle { anchors.fill: parent; anchors.margins: -5; radius: width / 2; color: "transparent"; border.color: Theme.accent; visible: orb.activeFocus }
        scale: orbArea.pressed ? 0.95 : 1
        Behavior on scale { NumberAnimation { duration: 90 } }
        // Soft bloom, as in the reference mark: a gradient, so it has no edge.
        Shape {
            anchors.centerIn: parent; width: 106; height: 106
            preferredRendererType: Shape.CurveRenderer
            opacity: orb.lit ? 1 : 0.72
            Behavior on opacity { NumberAnimation { duration: Theme.hoverMs } }
            ShapePath {
                strokeWidth: -1
                fillGradient: RadialGradient {
                    centerX: 53; centerY: 53; centerRadius: 53; focalX: 53; focalY: 53
                    GradientStop { position: 0.00; color: Theme.alpha(Theme.accent, 0.34) }
                    GradientStop { position: 0.42; color: Theme.alpha(Theme.accent, 0.15) }
                    GradientStop { position: 0.72; color: Theme.alpha(Theme.accent, 0.04) }
                    GradientStop { position: 1.00; color: "transparent" }
                }
                PathAngleArc { centerX: 53; centerY: 53; radiusX: 53; radiusY: 53; startAngle: 0; sweepAngle: 360 }
            }
        }
        // Outer calibrated ring with nodes.
        Item {
            anchors.fill: parent
            RotationAnimator on rotation { running: Theme.ambient && bar.settingsOpen; loops: Animation.Infinite; from: 0; to: 360; duration: 12000 }
            Rectangle { anchors.fill: parent; radius: width / 2; color: "transparent"; border.width: 1; border.color: Theme.accent; opacity: orb.lit ? 0.9 : 0.45; Behavior on opacity { NumberAnimation { duration: Theme.hoverMs } } }
            Repeater { model: 12
                Rectangle { width: index % 3 === 0 ? 3 : 1; height: 3; radius: index % 3 === 0 ? 1.5 : 0; color: Theme.accent; opacity: orb.lit ? 0.95 : 0.6; x: orb.width / 2 - width / 2; y: index % 3 === 0 ? -1 : 2
                    transform: Rotation { origin.x: width / 2; origin.y: orb.height / 2 - y; angle: index * 30 } } }
        }
        // The luminous mark: a thick broken cyan ring around a dark centre.
        Rectangle { anchors.centerIn: parent; width: 46; height: 46; radius: 23; color: Theme.bg0; opacity: 0.9 }
        Shape {
            anchors.centerIn: parent; width: 46; height: 46
            preferredRendererType: Shape.CurveRenderer
            opacity: orb.lit ? 1 : 0.92
            ShapePath { strokeColor: Theme.alpha(Theme.accent, 0.26); strokeWidth: 8; fillColor: "transparent"; capStyle: ShapePath.RoundCap
                PathAngleArc { centerX: 23; centerY: 23; radiusX: 16.5; radiusY: 16.5; startAngle: 350; sweepAngle: 150 } }
            ShapePath { strokeColor: Theme.accent; strokeWidth: 3.4; fillColor: "transparent"; capStyle: ShapePath.RoundCap
                PathAngleArc { centerX: 23; centerY: 23; radiusX: 16.5; radiusY: 16.5; startAngle: 350; sweepAngle: 150 } }
            ShapePath { strokeColor: Theme.accent; strokeWidth: 3.4; fillColor: "transparent"; capStyle: ShapePath.RoundCap
                PathAngleArc { centerX: 23; centerY: 23; radiusX: 16.5; radiusY: 16.5; startAngle: 166; sweepAngle: 122 } }
            ShapePath { strokeColor: Qt.lighter(Theme.accent, 1.5); strokeWidth: 1.4; fillColor: "transparent"; capStyle: ShapePath.RoundCap
                PathAngleArc { centerX: 23; centerY: 23; radiusX: 16.5; radiusY: 16.5; startAngle: 30; sweepAngle: 54 } }
        }
        Rectangle { anchors.centerIn: parent; width: 24; height: 24; radius: 12; color: Theme.bg0; opacity: 0.92 }
        Rectangle { anchors.centerIn: parent; width: 34; height: 34; radius: 17; color: "transparent"; border.width: 1; border.color: Theme.alpha(Theme.accent, 0.3) }
        MouseArea { id: orbArea; anchors.fill: parent; anchors.margins: -6; hoverEnabled: true; cursorShape: Qt.PointingHandCursor; onClicked: bar.openSettings() }
        Rectangle {
            visible: orb.hovered && !bar.settingsOpen
            anchors.top: parent.bottom; anchors.topMargin: 10; anchors.horizontalCenter: parent.horizontalCenter
            width: tipText.width + 16; height: 24; radius: Theme.radiusTiny; color: Theme.surfaceRaised; border.width: 1; border.color: Theme.borderStrong; z: 50
            Text { id: tipText; anchors.centerIn: parent; text: qsTr("Settings"); color: Theme.text; font: Theme.caption }
        }
    }
    Text { id: wordmark; objectName: "brandWordmark"; x: 120; y: 21; text: "JARVIS"; color: Theme.text; font: Theme.brand
           lineHeightMode: Text.FixedHeight; lineHeight: 34 }
    Text { id: tagline; objectName: "brandTagline"; x: 122; y: 55; text: qsTr("THINKS AHEAD FOR YOU"); color: Theme.textLabel; font: Theme.hudLabel
           lineHeightMode: Text.FixedHeight; lineHeight: 16 }

    // The project selector and "+ New conversation" (V2.0 Gate 6B, Salih
    // decision 2026-09-30; supersedes the 6A badge). The selector names the
    // open conversation's owner (or Global) and opens a menu: active projects,
    // Global, All projects. Choosing one only OPENS a conversation (its newest
    // non-archived one, or a new one) - it never changes a conversation's
    // owner, and it grants, approves or selects nothing: Core binds a session
    // to its conversation's owner. Mouse first; the keyboard reaches it too
    // (Gate 6E, Q7): Tab, Return / Space / Down, Menu or Shift+F10.
    //
    // Layout (Gate 6E): inline after the wordmark while it fits; on a narrow
    // window it takes its own row under the tagline, beside the rail card. The
    // switcher gives way first, then the selector; neither name is ever cut to
    // a bare ellipsis, and an elided name has its tip.
    Item {
        id: scopeGroup
        objectName: "projectScope"
        parent: bar.controlLayer || bar
        visible: bridge.page === "home"
        readonly property var owner: bridge.conversationProject || ({})
        readonly property color tone: owner.archived ? Theme.warning : owner.global ? Theme.textLabel : Theme.accent
        // An edit of a name is bound to the conversation it STARTED on.
        property string editId: ""

        // Geometry. A pill is: padding, its lead (the selector's dot or, when
        // archived, the ARŞİV badge in its place), the name, the chevron.
        readonly property real gap: twoRow ? 6 : 8
        readonly property real pad: twoRow ? 10 : 12
        readonly property real chevronRoom: 6 + 10 + pad
        readonly property real lead: (owner.archived ? archivedBadge.width : 6) + 6
        readonly property real selFixed: pad + lead + chevronRoom
        readonly property real swFixed: pad + chevronRoom
        readonly property real selNat: Math.min(280, selFixed + selectorName.implicitWidth)
        readonly property real swNat: Math.min(320, swFixed + switcherText.implicitWidth)
        // Floors: never under six characters of a name (then the ellipsis).
        TextMetrics { id: selSix; font: Theme.caption; text: (scopeGroup.owner.name || "").slice(0, 6) + "…" }
        TextMetrics { id: swSix; font: Theme.caption; text: switcher.shown.slice(0, 6) + "…" }
        readonly property real selFloor: Math.min(selNat, selFixed + Math.ceil(selSix.advanceWidth) + 2)
        readonly property real swFloor: Math.min(swNat, swFixed + Math.ceil(swSix.advanceWidth) + 2)
        readonly property real selMin: Math.max(selFloor, Math.min(selNat, 110))
        readonly property real swMin: Math.max(swFloor, Math.min(swNat, 140))
        readonly property real newFullW: newFull.implicitWidth + 24
        // Inline after the wordmark while both names keep their room; else a
        // row of its own under the tagline (the rail card's right edge + 12;
        // the Command preset has no rail card: under the orb).
        readonly property real inlineX: wordmark.x + wordmark.width + 16
        readonly property bool twoRow: bar.narrow || core.x - inlineX - 6
                                       < 110 + 140 + Theme.pillHeight + 16 + (owner.archived ? archivedBadge.width : 0)
        x: twoRow ? (Theme.command ? orb.x : bar.railWidth + 2) : inlineX
        y: twoRow ? tagline.y + 24 : wordmark.y + 2
        readonly property real room: core.x - x - 6
        readonly property bool newIcon: twoRow || room < selMin + swMin + newFullW + 2 * gap
        readonly property real newW: newIcon ? Theme.pillHeight : newFullW
        readonly property real avail: room - newW - 2 * gap
        // The switcher gives way first (to 140), then the selector (to 110),
        // then both to their six-character floors.
        readonly property real swFit: Math.max(swMin, Math.min(swNat, avail - selNat))
        readonly property real selFit: Math.max(selMin, Math.min(selNat, avail - swFit))
        readonly property real swW: Math.max(swFloor, Math.min(swFit, avail - selFit))
        readonly property real selW: Math.max(selFloor, Math.min(selFit, avail - swW))
        height: Theme.pillHeight
        width: selW + gap + swW + gap + newW

        ScopePill {
            id: projectSelector
            objectName: "projectSelector"
            width: scopeGroup.selW
            idleFill: Theme.alpha(scopeGroup.tone, 0.07); hotFill: Theme.alpha(scopeGroup.tone, 0.14)
            border.color: Theme.alpha(scopeGroup.tone, 0.55)
            open: selectorMenu.visible
            Accessible.role: Accessible.ButtonMenu; Accessible.name: selectorText.text
            onActivated: selectorMenu.visible ? selectorMenu.close() : selectorMenu.open()
            onDismissRequested: selectorMenu.close()
            Rectangle { visible: !scopeGroup.owner.archived; x: scopeGroup.pad; anchors.verticalCenter: parent.verticalCenter
                        width: 6; height: 6; radius: 3; color: scopeGroup.tone }
            Rectangle {
                id: archivedBadge
                objectName: "projectSelectorArchived"
                visible: !!scopeGroup.owner.archived
                x: scopeGroup.pad - 2; anchors.verticalCenter: parent.verticalCenter
                width: badgeText.implicitWidth + 10; height: 18; radius: 9
                color: Theme.alpha(Theme.warning, 0.12); border.width: 1; border.color: Theme.alpha(Theme.warning, 0.45)
                Text { id: badgeText; anchors.centerIn: parent; text: qsTr("ARCHIVED"); color: Theme.warning; font: Theme.label }
            }
            Item {
                id: selectorText
                objectName: "projectSelectorText"
                // The owner's whole label ("name · archived"): what the pill says
                // and what assistive technology reads. It is drawn as the name
                // and, when archived, the ARŞİV badge - never colour alone.
                readonly property string text: (scopeGroup.owner.name || "")
                                               + (scopeGroup.owner.archived ? " · " + qsTr("archived") : "")
                x: scopeGroup.pad + scopeGroup.lead - (scopeGroup.owner.archived ? 2 : 0); height: parent.height
                width: Math.max(0, projectSelector.width - x - scopeGroup.chevronRoom)
                Text {
                    id: selectorName
                    objectName: "projectSelectorName"
                    anchors.verticalCenter: parent.verticalCenter
                    width: Math.min(implicitWidth, parent.width)
                    textFormat: Text.PlainText; elide: Text.ElideRight
                    text: scopeGroup.owner.name || ""
                    color: scopeGroup.owner.global ? Theme.textLabel : Theme.text; font: Theme.caption
                }
            }
            Chevron { objectName: "projectSelectorChevron"; open: selectorMenu.visible
                      anchors { right: parent.right; rightMargin: scopeGroup.pad; verticalCenter: parent.verticalCenter } }
            HoverTip { objectName: "projectSelectorTip"; text: selectorText.text; active: selectorName.truncated && !selectorMenu.visible }
        }

        // Gate 6D-a (Salih decision 2026-09-30): the open conversation's name.
        // Click: the current scope's conversations, to switch. Right-click:
        // rename. A label only - it selects, grants and approves nothing.
        ScopePill {
            id: switcher
            objectName: "conversationSwitcher"
            readonly property string shown: bridge.conversationTitle || qsTr("New Chat")
            readonly property bool renameAllowed: bridge.conversationId !== "" && !bridge.conversationReadOnly && !bridge.busy
            readonly property string renameReason: bridge.conversationId === "" ? qsTr("This conversation is not saved yet.")
                : bridge.conversationArchived ? qsTr("This conversation is archived: unarchive it to rename it.")
                : bridge.conversationReadOnly ? qsTr("The project is archived: unarchive it to rename this conversation.")
                : bridge.busy ? qsTr("Wait for the current run to finish.") : ""
            // Gate 6D-b: archive (reversible), then the permanent delete - offered
            // only on an archived conversation or under an archived project.
            readonly property bool archiveAllowed: renameAllowed
            readonly property string archiveReason: bridge.conversationId === "" ? qsTr("This conversation is not saved yet.")
                : bridge.conversationArchived ? qsTr("This conversation is already archived.")
                : bridge.conversationReadOnly ? qsTr("The project is archived: its conversations are already read-only.")
                : bridge.busy ? qsTr("Wait for the current run to finish.") : ""
            readonly property bool deleteAllowed: bridge.conversationId !== "" && !bridge.busy
                                                  && (bridge.conversationArchived || !!scopeGroup.owner.archived)
            readonly property string deleteReason: bridge.conversationId === "" ? qsTr("This conversation is not saved yet.")
                : bridge.busy ? qsTr("Wait for the current run to finish.")
                : !deleteAllowed ? qsTr("Archive it first") : ""
            x: projectSelector.width + scopeGroup.gap
            width: scopeGroup.swW
            border.color: Theme.borderStrong
            open: switcherMenu.visible || switcherContext.visible
            Accessible.role: Accessible.ButtonMenu; Accessible.name: switcherText.text
            onActivated: { switcherContext.close(); switcherMenu.visible ? switcherMenu.close() : switcherMenu.open() }
            onContextRequested: { switcherMenu.close(); switcherContext.open() }
            onDismissRequested: { switcherMenu.close(); switcherContext.close() }
            Text {
                id: switcherText
                objectName: "conversationSwitcherText"
                x: scopeGroup.pad; anchors.verticalCenter: parent.verticalCenter
                width: Math.max(0, Math.min(implicitWidth, switcher.width - scopeGroup.swFixed))
                textFormat: Text.PlainText; elide: Text.ElideRight
                text: switcher.shown
                color: bridge.conversationTitle ? Theme.text : Theme.textLabel; font: Theme.caption
            }
            Chevron { objectName: "conversationSwitcherChevron"; open: switcherMenu.visible
                      anchors { right: parent.right; rightMargin: scopeGroup.pad; verticalCenter: parent.verticalCenter } }
            HoverTip { objectName: "conversationSwitcherTipText"; text: bridge.conversationTitle
                       active: bridge.conversationTitle !== "" && !switcherMenu.visible && !switcherContext.visible }
        }

        ScopePill {
            id: newChatButton
            objectName: "newConversationButton"
            // Disabled with its reason while the open conversation's project
            // is archived (the store refuses a new conversation there anyway).
            // An archived GLOBAL conversation does not stop a new one.
            readonly property bool allowed: !scopeGroup.owner.archived && !bridge.busy
            readonly property string reason: scopeGroup.owner.archived
                ? qsTr("The project is archived: unarchive it to start a new conversation.")
                : bridge.busy ? qsTr("Wait for the current run to finish.") : ""
            x: switcher.x + switcher.width + scopeGroup.gap
            width: scopeGroup.newW
            menuKeys: false
            opacity: allowed ? 1 : 0.45
            hotFill: allowed ? Theme.alpha(Theme.accent, 0.14) : idleFill
            border.color: Theme.borderStrong
            Accessible.role: Accessible.Button; Accessible.name: newFull.text; Accessible.description: reason
            onActivated: if (newChatButton.allowed) bridge.newConversationInScope()
            Text { id: newFull; visible: !scopeGroup.newIcon; anchors.centerIn: parent; text: qsTr("+ New conversation")
                   color: Theme.text; font: Theme.caption }
            // The icon form: a drawn plus, no font glyph.
            Item {
                objectName: "newConversationIcon"
                visible: scopeGroup.newIcon; anchors.centerIn: parent; width: 10; height: 10
                Rectangle { anchors.centerIn: parent; width: 10; height: 1.6; radius: 0.8; color: Theme.text }
                Rectangle { anchors.centerIn: parent; width: 1.6; height: 10; radius: 0.8; color: Theme.text }
            }
            // The reason (or the full label of the icon form), at full opacity
            // even while the button itself is dimmed.
            HoverTip { objectName: "newConversationTip"; text: newChatButton.reason || newFull.text
                       active: newChatButton.reason !== "" || scopeGroup.newIcon }
        }

        AttachDialog { id: attachDialog }
        ConversationDeleteDialog { id: deleteDialog }

        // The current scope's conversations, newest first; the open one marked.
        ScopeMenu {
            id: switcherMenu
            objectName: "conversationSwitcherMenu"
            restY: switcher.height + 8; x: switcher.x
            width: 320
            contentItem: Column {
                spacing: 2
                Text { text: qsTr("CONVERSATIONS"); color: Theme.textMuted; font: Theme.label; leftPadding: 12; bottomPadding: 4 }
                ListView {
                    objectName: "conversationSwitcherList"
                    width: switcherMenu.availableWidth; height: Math.min(contentHeight, 8 * (Theme.menuRowHeight + 2) - 2)
                    clip: true; spacing: 2
                    boundsBehavior: Flickable.StopAtBounds
                    ScrollBar.vertical: ScrollBar { }
                    model: bridge.scopeConversations
                    delegate: ScopeChoice { objectName: "conversationSwitcherItem"; width: ListView.view.width
                                            label: modelData.title || qsTr("New Chat"); muted: !modelData.title; current: modelData.active
                                            onChosen: { switcherMenu.close(); bridge.openConversation(modelData.id) } }
                }
            }
        }

        // Right-click: rename (6D-a), archive and the permanent delete (6D-b).
        // Each is disabled with its reason (archived project or conversation,
        // not archived yet, a busy run); the store still refuses. Each acts on
        // the conversation open when it was chosen.
        ScopeMenu {
            id: switcherContext
            objectName: "conversationSwitcherContext"
            restY: switcher.height + 8; x: switcher.x
            width: 300
            onAboutToShow: bridge.previewDeleteConversation("")
            contentItem: Column {
                spacing: 2
                // Which conversation these act on.
                Text { objectName: "conversationContextHeader"; width: switcherContext.availableWidth; leftPadding: 12; rightPadding: 12
                       bottomPadding: 4; elide: Text.ElideRight; textFormat: Text.PlainText
                       text: switcher.shown; color: Theme.textMuted; font: Theme.caption }
                ScopeChoice { objectName: "conversationRenameItem"; width: switcherContext.availableWidth
                              label: qsTr("Rename"); available: switcher.renameAllowed
                              onChosen: { switcherContext.close(); scopeGroup.editId = bridge.conversationId
                                          bridge.clearRenameNotice(); switcherEditor.open() } }
                Reason { width: switcherContext.availableWidth; objectName: "conversationRenameReason"; visible: !switcher.renameAllowed; text: switcher.renameReason }
                ScopeChoice { objectName: "conversationArchiveItem"; width: switcherContext.availableWidth
                              label: qsTr("Archive"); available: switcher.archiveAllowed
                              onChosen: { var id = bridge.conversationId; if (bridge.archiveConversation(id)) switcherContext.close() } }
                Reason { width: switcherContext.availableWidth; objectName: "conversationArchiveReason"; visible: !switcher.archiveAllowed; text: switcher.archiveReason }
                Divider { width: switcherContext.availableWidth }
                ScopeChoice { objectName: "conversationDeleteItem"; width: switcherContext.availableWidth; danger: true
                              label: qsTr("Delete permanently…"); available: switcher.deleteAllowed
                              onChosen: { var id = bridge.conversationId; switcherContext.close(); deleteDialog.openFor(id) } }
                Reason { width: switcherContext.availableWidth; objectName: "conversationDeleteReason"; visible: !switcher.deleteAllowed; text: switcher.deleteReason }
                Text { objectName: "conversationLifecycleNotice"; visible: bridge.conversationNotice !== ""
                       width: switcherContext.availableWidth; leftPadding: 12; rightPadding: 12; wrapMode: Text.Wrap
                       textFormat: Text.PlainText; text: bridge.conversationNotice; color: Theme.warning; font: Theme.caption }
            }
        }

        // The edit. Enter / Save commits the normalized name to the conversation
        // the edit STARTED on; Escape / Cancel / a change of conversation reverts.
        ScopeMenu {
            id: switcherEditor
            objectName: "conversationRenameEditor"
            restY: switcher.height + 8; x: switcher.x
            width: 320; padding: 12
            closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
            onAboutToShow: { renameField.text = bridge.conversationTitle }
            onOpened: { renameField.forceActiveFocus(); renameField.selectAll() }
            function commit() {
                if (!bridge.busy && bridge.renameConversation(scopeGroup.editId, renameField.text)) switcherEditor.close()
            }
            Connections {
                target: bridge
                function onConversationChanged() {
                    if (switcherEditor.visible && bridge.conversationId !== scopeGroup.editId) switcherEditor.close()
                }
            }
            contentItem: Column {
                spacing: 10
                Text { objectName: "conversationRenameTitle"; text: i18n.upper(qsTr("Rename")); color: Theme.textMuted; font: Theme.label }
                TextField {
                    id: renameField
                    objectName: "conversationRenameField"
                    width: switcherEditor.availableWidth; height: 36; selectByMouse: true; color: Theme.text; font: Theme.body
                    placeholderText: qsTr("Conversation name")
                    background: Rectangle { radius: 6; color: Theme.surfaceDeep; border.width: 1
                                            border.color: renameField.activeFocus ? Theme.commandEdge : Theme.border }
                    onAccepted: switcherEditor.commit()
                }
                Text { objectName: "conversationRenameNotice"; visible: bridge.conversationRenameNotice !== ""
                       width: switcherEditor.availableWidth; wrapMode: Text.Wrap; textFormat: Text.PlainText
                       text: bridge.conversationRenameNotice; color: Theme.warning; font: Theme.caption }
                Item {
                    width: switcherEditor.availableWidth; height: 32
                    Row {
                        anchors.right: parent.right
                        spacing: 8
                        ActionButton { objectName: "conversationRenameCancel"; label: qsTr("Cancel"); height: 32
                                       onClicked: switcherEditor.close() }
                        ActionButton { objectName: "conversationRenameSave"; label: qsTr("Save"); primary: true; height: 32
                                       enabled: !bridge.busy; onClicked: switcherEditor.commit() }
                    }
                }
            }
        }

        // Projects first, then Global, then the two actions (in accent).
        ScopeMenu {
            id: selectorMenu
            objectName: "projectSelectorMenu"
            property var choices: []
            restY: projectSelector.height + 8; x: 0
            width: 300
            onAboutToShow: choices = bridge.projectChoices()
            contentItem: Column {
                spacing: 2
                Text { text: qsTr("PROJECTS"); color: Theme.textMuted; font: Theme.label; leftPadding: 12; bottomPadding: 4 }
                Repeater {
                    model: selectorMenu.choices
                    delegate: ScopeChoice { objectName: "projectSelectorItem"; width: selectorMenu.availableWidth; label: modelData.name
                                            current: modelData.id === scopeGroup.owner.id
                                            onChosen: { selectorMenu.close(); bridge.openProjectScope(modelData.id) } }
                }
                Divider { width: selectorMenu.availableWidth }
                ScopeChoice { objectName: "projectSelectorGlobal"; width: selectorMenu.availableWidth
                              label: qsTr("Global"); current: !!scopeGroup.owner.global
                              onChosen: { selectorMenu.close(); bridge.openProjectScope("") } }
                Divider { width: selectorMenu.availableWidth }
                // Gate 6C: only while the open conversation is a writable Global one.
                ScopeChoice { objectName: "projectSelectorAttach"; visible: bridge.canAttach; width: selectorMenu.availableWidth
                              label: qsTr("Attach to a project…"); action: true
                              onChosen: { selectorMenu.close(); attachDialog.openDialog() } }
                ScopeChoice { objectName: "projectSelectorAll"; width: selectorMenu.availableWidth; action: true
                              label: qsTr("All projects"); onChosen: { selectorMenu.close(); bridge.showProject("") } }
            }
        }
    }

    LeftContextCard {
        visible: !Theme.command
        x: 10
        width: bar.railWidth - 20
        // The accepted summary rail stays fixed; leave a deliberate 12 px gap.
        height: Math.min(144, Math.round(bar.viewportHeight * 0.1980) + 76 - 12 - 84)
        y: Math.round(bar.viewportHeight * 0.1980) + 76 - 12 - height
        now: bar.now
    }

    // -- core + core-side status ----------------------------------------------
    JarvisCore {
        id: core
        objectName: "jarvisCore"
        x: bar.coreCx - width / 2
        y: bar.coreCy - height / 2
        width: bar.coreDiameter; height: width
        state_: bridge.coreState
        active: Window.active
        environment: !bar.narrow && !Theme.command
        Behavior on width { NumberAnimation { duration: Theme.reflow; easing.type: Easing.OutCubic } }
    }
    // The core's companion: ONE complete label, immediately right of the core
    // and clearly subordinate to it, for where the installation STANDS - an
    // outcome (completed, error, outcome unknown) or the frozen safety state.
    // What is in progress - preparing, executing, waiting for an approval or an
    // answer - is said once, in the conversation, on the live turn it belongs
    // to (FeedView), never a second time here; the core's own light still shows
    // every one of those states. Idle says nothing: readiness is not claimed,
    // and no voice or listening is implied anywhere. It wraps whole words and
    // is never cut short.
    Item {
        id: ticker
        objectName: "coreStatusGroup"
        readonly property bool inProgress: bridge.coreState === "running" || bridge.coreState === "awaiting_approval"
                                          || bridge.coreState === "awaiting_input"
        readonly property string label: inProgress ? "" : Theme.stateLabel(bridge.coreState)
        visible: bar.tickerFits && label !== ""
        x: bar.ticker0
        y: bar.coreCy - height / 2
        // Never under the selector: the label stops short of the strip.
        width: Math.max(bar.tickerNeed, Math.min(240, controls.x - x - 12)); height: companion.height
        Text {
            id: companion
            objectName: "coreStatusLabel"
            width: parent.width
            text: ticker.label
            wrapMode: Text.WordWrap; maximumLineCount: 2
            color: bridge.coreState === "running" ? Theme.materialGlint : Theme.stateColor(bridge.coreState)
            font: Theme.caption
            lineHeightMode: Text.FixedHeight; lineHeight: 18
            Behavior on color { ColorAnimation { duration: 200 } }
        }
    }

    // -- lifecycle progress ------------------------------------------------------
    // The current request's four stages, quiet in the header between the
    // wordmark and the core: filled when done, lit with a faint halo for the
    // current stage, hollow and dim for what is still ahead. Projected by the
    // bridge (bridge.progress) from authoritative state and shown only while
    // there is real work - never a ghost at idle. No chrome, no motion.
    Item {
        id: progress
        objectName: "hudProgress"
        readonly property var stages: bridge.progress.stages || []
        // Ends just short of the core; reaches toward the rail, at most 360 px.
        readonly property real endX: bar.coreCx - bar.coreDiameter * 0.5 - bar.width * 0.02
        // Gate 6E: on a narrow window the scope controls share this band, and
        // controls outrank secondary metadata (the degradation order above):
        // the indicator starts after them, or yields when that leaves too little.
        readonly property real startX: scopeGroup.visible && scopeGroup.twoRow ? scopeGroup.x + scopeGroup.width + 16
                                                                               : bar.railWidth + 24
        visible: bridge.progress.active === true && stages.length === 4 && width >= 240
        width: Math.max(0, Math.min(360, endX - startX)); height: 50
        x: endX - width
        y: bar.coreCy + Math.round(bar.viewportHeight * 0.03) - 6
        readonly property real pitch: width / 4
        Repeater {
            model: 3
            Rectangle { x: progress.pitch * (index + 0.5) + 10; y: 5.5; width: progress.pitch - 20; height: 1
                        color: progress.stages[index] === "done" ? Theme.alpha(Theme.accent, 0.5) : Theme.alpha(Theme.hud, 0.3) }
        }
        Repeater {
            model: 4
            Item {
                id: step
                objectName: "hudStage_" + index
                x: progress.pitch * index; width: progress.pitch; height: parent.height
                readonly property string s: progress.stages[index] || "pending"
                readonly property bool current: ["running", "waiting", "error", "unknown", "denied"].indexOf(s) >= 0
                readonly property color tone: s === "waiting" ? Theme.warning : s === "unknown" ? Theme.unknownEdge
                                              : s === "error" || s === "denied" ? Theme.danger : Theme.accent
                Accessible.role: Accessible.StaticText
                Accessible.name: label.text + ": " + ({ pending: qsTr("Pending"), waiting: qsTr("Awaiting you"), running: qsTr("Running"), done: qsTr("Completed"),
                                                        error: qsTr("Failed"), unknown: qsTr("Outcome unknown"), denied: qsTr("Denied"), skipped: qsTr("Not run") })[s]
                Rectangle { visible: step.current; x: (parent.width - width) / 2; y: -4; width: 20; height: 20; radius: 10; color: Theme.alpha(step.tone, 0.18) }
                Rectangle {
                    objectName: "hudStageNode_" + index
                    x: (parent.width - width) / 2; y: 0; width: 12; height: 12; radius: 6
                    color: step.s === "done" ? Theme.alpha(Theme.accent, 0.8) : step.current ? step.tone : "transparent"
                    border.width: step.s === "done" || step.current ? 0 : 1.5
                    border.color: Theme.alpha(Theme.textLabel, 0.5)
                }
                Text {
                    id: label
                    x: -5; y: 19; width: parent.width + 10
                    horizontalAlignment: Text.AlignHCenter; wrapMode: Text.WordWrap; maximumLineCount: 2; elide: Text.ElideRight
                    text: [qsTr("Understanding your request"), qsTr("Authorization"), qsTr("Execution"), qsTr("Outcome")][index]
                    color: step.current ? step.tone : step.s === "done" ? Theme.text : Theme.textLabel
                    opacity: step.current || step.s === "done" ? 1 : 0.75
                    font: Theme.metadata; lineHeightMode: Text.FixedHeight; lineHeight: 15
                }
            }
        }
    }

    // -- the command strip ----------------------------------------------------
    // One horizontal command assembly: selector, status instrument, kill
    // switch, navigation grid, then the window chrome behind a divider. The
    // right edge is anchored; the selector takes the space it needs to the
    // left. Every element reads real state and nothing here duplicates it.
    Row {
        id: controls
        objectName: "commandStrip"
        parent: bar.controlLayer || bar
        anchors { right: parent.right; top: parent.top
                  rightMargin: Math.round(bar.width * Theme.macroStripRight); topMargin: 22 }
        height: Theme.stripHeight
        spacing: bar.stripGap
        readonly property real mid: Theme.stripHeight / 2

        ModelSelector {
            id: modelChip
            objectName: "modelTrigger"
            y: controls.mid - height / 2
            width: bar.selectorWidth; height: Theme.stripSelectorHeight
            open: bar.modelOpen
            // One shape for every provider: the SHORT model name, then
            // BRAND | EFFORT, then the plan summary - all real reported state
            // (bridge.modelCard). The canonical model id is unchanged by this.
            readonly property var card: bridge.modelCard || ({})
            // Claude with nothing configured really is "whatever the CLI picks";
            // Codex has no default at all, so there the truth is "no model chosen".
            modelName: card.name ? card.name : card.provider === "claude" ? qsTr("CLI default") : qsTr("No model chosen")
            metadata: card.meta || ""
            quota: card.quota || []
            onActivated: bar.openModel()
        }

        // Execution state: the glass lamp, at instrument scale. It indicates
        // only - the Kill Switch beside it is the control.
        Item {
            width: Theme.stripStatus; height: Theme.stripHeight
            y: controls.mid - height / 2
            StatusOrb {
                id: statusOrb
                objectName: "statusOrb"
                anchors.centerIn: parent
                diameter: Theme.stripStatus
                // An unconfirmed change never shows the reassuring enabled glow.
                lit: bridge.executionState === "running"
                skin: bridge.statusIndicator
                Accessible.role: Accessible.Indicator
                Accessible.name: bridge.executionState === "unconfirmed" ? qsTr("JARVIS status: unconfirmed") : bridge.configRefused ? qsTr("JARVIS status: configuration refused") : bridge.executionState === "frozen" ? qsTr("JARVIS status: frozen") : bridge.executionState === "freeze_requested" ? qsTr("JARVIS status: freeze requested") : (bridge.busy ? qsTr("JARVIS status: running an operation") : qsTr("JARVIS status: ready"))
                MouseArea { id: orbHover; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton }
                Rectangle {
                    visible: orbHover.containsMouse
                    anchors.top: parent.bottom; anchors.topMargin: 2; anchors.horizontalCenter: parent.horizontalCenter
                    width: orbTip.width + 16; height: 24; radius: Theme.radiusTiny; color: Theme.surfaceRaised; border.width: 1; border.color: Theme.borderStrong; z: 50
                    Text { id: orbTip; anchors.centerIn: parent; color: Theme.text; font: Theme.caption
                        text: bridge.executionState === "unconfirmed" ? qsTr("JARVIS state unconfirmed") : bridge.configRefused ? qsTr("Configuration refused") : bridge.executionState === "frozen" ? qsTr("JARVIS frozen") : bridge.executionState === "freeze_requested" ? qsTr("Freeze requested") : (bridge.busy ? qsTr("Running · operation in progress") : qsTr("Running")) }
                }
            }
        }

        // The kill switch: a separate control with its own hit target,
        // deliberately smaller than the status instrument and unmistakably
        // an action rather than a state.
        Item {
            id: killSlot
            width: Theme.stripKill; height: Theme.stripHeight
            y: controls.mid - height / 2
            PowerControl {
                id: killControl
                objectName: "killSwitchControl"
                anchors.centerIn: parent
                diameter: Theme.stripKill
                enabled: !bridge.configRefused
                // Latched only when the store confirmed execution is off.
                engaged: bridge.executionState === "frozen"
                onClicked: bar.lifecycleRequested()
            }
            // Its meaning, in words: Freeze / Resume JARVIS - not close, not quit, not cancel.
            // A micro-label under the control; it does not move the strip.
            Text {
                objectName: "killSwitchLabel"
                anchors { horizontalCenter: killControl.horizontalCenter; top: killControl.bottom; topMargin: 5 }
                horizontalAlignment: Text.AlignHCenter
                text: ({ frozen: qsTr("FROZEN"), freeze_requested: qsTr("FREEZING"), resuming: qsTr("RESUMING"), unconfirmed: qsTr("STATE\nUNCONFIRMED"), refused: qsTr("FROZEN") })[bridge.executionState] || qsTr("FREEZE")
                color: bridge.executionState === "running" || bridge.executionState === "unknown" ? Theme.textLabel : Theme.danger
                font: Theme.label
                lineHeight: 0.95
            }
        }

        IconButton {
            id: navigationTrigger
            objectName: "navigationTrigger"
            y: controls.mid - height / 2
            tip: qsTr("Navigation"); width: 38; height: 38; onClicked: bar.menuRequested()
            Grid { anchors.centerIn: parent; columns: 2; spacing: 4
                Repeater { model: 4; Rectangle { width: 6; height: 6; radius: index === 3 ? 1 : 3; color: "transparent"; border.width: 1.4; border.color: Theme.accent } } }
        }

        // The window chrome sits behind a divider and stays restrained, so
        // it never competes with the JARVIS controls.
        Item {
            width: 13; height: Theme.stripHeight
            y: controls.mid - height / 2
            Rectangle { anchors.centerIn: parent; width: 1; height: 26; color: Theme.borderStrong; opacity: 0.8 }
        }
        Row {
            y: controls.mid - height / 2
            spacing: 2
            IconButton { glyph: "–"; tip: qsTr("Minimize"); width: 34; height: 34; onClicked: bar.minimizeRequested() }
            IconButton { glyph: bar.fullscreen ? "❐" : "▢"; tip: bar.fullscreen ? qsTr("Windowed") : qsTr("Full screen"); width: 34; height: 34; onClicked: bar.restoreRequested() }
            IconButton { objectName: "windowCloseControl"; glyph: "✕"; tip: bridge.trayAvailable ? qsTr("Hide to tray - JARVIS keeps running") : qsTr("Close"); width: 34; height: 34; onClicked: bar.closeRequested() }
        }
    }

    // The tiny icon-only layout-edit control: a sliders glyph in a framed square.
    IconButton {
        anchors { right: parent.right; top: parent.top
                  rightMargin: Math.round(bar.width * Theme.macroStripRight) + 8
                  topMargin: Math.round(bar.viewportHeight * 0.1130) }
        objectName: "editLayoutTrigger"
        tip: bridge.editMode ? qsTr("Done editing") : qsTr("Edit workspace layout")
        active: bridge.editMode
        framed: true
        width: 34; height: 34
        onClicked: bar.editRequested()
        Item { anchors.centerIn: parent; width: 16; height: 12
            property color c: bridge.editMode ? Theme.accent : Theme.accent
            Rectangle { x: 0; y: 2; width: 16; height: 1.2; color: parent.c }   Rectangle { x: 9; y: 0; width: 5; height: 5; radius: 2.5; color: Theme.bg0; border.width: 1.2; border.color: parent.c }
            Rectangle { x: 0; y: 10; width: 16; height: 1.2; color: parent.c }  Rectangle { x: 3; y: 8; width: 5; height: 5; radius: 2.5; color: Theme.bg0; border.width: 1.2; border.color: parent.c }
        }
    }
}
