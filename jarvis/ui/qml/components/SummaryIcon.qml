import QtQuick
import QtQuick.Shapes

// The summary line icons (tasks / updates / focus / plan / memory), drawn at
// 18 px so the rail cards and their peek panels share one set; and the
// sidebar's scope icons (global, project - Gate 6F) and its pin (F6).
Shape {
    id: glyph
    property string icon: "tasks"
    property color tint: "#f2f8ff"
    width: 18; height: 18
    preferredRendererType: Shape.CurveRenderer
    // Tasks: a clipboard with two lines.
    ShapePath {
        strokeColor: glyph.icon === "tasks" ? glyph.tint : "transparent"; strokeWidth: 1.6; fillColor: "transparent"
        joinStyle: ShapePath.RoundJoin; capStyle: ShapePath.RoundCap
        startX: 5; startY: 3
        PathLine { x: 3; y: 3 } PathLine { x: 3; y: 17 } PathLine { x: 15; y: 17 } PathLine { x: 15; y: 3 } PathLine { x: 13; y: 3 }
        PathMove { x: 6; y: 1.5 } PathLine { x: 12; y: 1.5 } PathLine { x: 12; y: 4.5 } PathLine { x: 6; y: 4.5 } PathLine { x: 6; y: 1.5 }
        PathMove { x: 6; y: 9 } PathLine { x: 12; y: 9 }
        PathMove { x: 6; y: 13 } PathLine { x: 12; y: 13 }
    }
    // Updates: an envelope.
    ShapePath {
        strokeColor: glyph.icon === "updates" ? glyph.tint : "transparent"; strokeWidth: 1.6; fillColor: "transparent"
        joinStyle: ShapePath.RoundJoin; capStyle: ShapePath.RoundCap
        startX: 1.5; startY: 4
        PathLine { x: 16.5; y: 4 } PathLine { x: 16.5; y: 14.5 } PathLine { x: 1.5; y: 14.5 } PathLine { x: 1.5; y: 4 }
        PathLine { x: 9; y: 10 } PathLine { x: 16.5; y: 4 }
    }
    // Focus: a target.
    ShapePath {
        strokeColor: glyph.icon === "focus" ? glyph.tint : "transparent"; strokeWidth: 1.6; fillColor: "transparent"
        capStyle: ShapePath.RoundCap
        PathAngleArc { centerX: 9; centerY: 9.5; radiusX: 7; radiusY: 7; startAngle: 0; sweepAngle: 360 }
        PathMove { x: 9; y: 0.5 } PathLine { x: 9; y: 3 }
    }
    ShapePath {
        strokeWidth: -1; fillColor: glyph.icon === "focus" ? glyph.tint : "transparent"
        PathAngleArc { centerX: 9; centerY: 9.5; radiusX: 2.2; radiusY: 2.2; startAngle: 0; sweepAngle: 360 }
    }
    // Plan: a bookmark with a mark.
    ShapePath {
        strokeColor: glyph.icon === "plan" ? glyph.tint : "transparent"; strokeWidth: 1.6; fillColor: "transparent"
        joinStyle: ShapePath.RoundJoin; capStyle: ShapePath.RoundCap
        startX: 3.5; startY: 1.5
        PathLine { x: 14.5; y: 1.5 } PathLine { x: 14.5; y: 16.5 } PathLine { x: 9; y: 12.5 } PathLine { x: 3.5; y: 16.5 } PathLine { x: 3.5; y: 1.5 }
        PathMove { x: 9; y: 4.8 } PathLine { x: 9; y: 8.6 }
        PathMove { x: 7.1; y: 6.7 } PathLine { x: 10.9; y: 6.7 }
    }
    // Memory: stacked records - a lidded cylinder with one seam.
    ShapePath {
        strokeColor: glyph.icon === "memory" ? glyph.tint : "transparent"; strokeWidth: 1.6; fillColor: "transparent"
        joinStyle: ShapePath.RoundJoin; capStyle: ShapePath.RoundCap
        PathAngleArc { centerX: 9; centerY: 4; radiusX: 6.5; radiusY: 2.5; startAngle: 0; sweepAngle: 360; moveToStart: true }
        PathMove { x: 2.5; y: 4 } PathLine { x: 2.5; y: 14 }
        PathMove { x: 15.5; y: 4 } PathLine { x: 15.5; y: 14 }
        PathAngleArc { centerX: 9; centerY: 9; radiusX: 6.5; radiusY: 2.5; startAngle: 0; sweepAngle: 180; moveToStart: true }
        PathAngleArc { centerX: 9; centerY: 14; radiusX: 6.5; radiusY: 2.5; startAngle: 0; sweepAngle: 180; moveToStart: true }
    }
    // Global: a globe - the outline, the equator and one meridian.
    ShapePath {
        strokeColor: glyph.icon === "global" ? glyph.tint : "transparent"; strokeWidth: 1.6; fillColor: "transparent"
        capStyle: ShapePath.RoundCap
        PathAngleArc { centerX: 9; centerY: 9; radiusX: 7; radiusY: 7; startAngle: 0; sweepAngle: 360; moveToStart: true }
        PathMove { x: 2; y: 9 } PathLine { x: 16; y: 9 }
        PathAngleArc { centerX: 9; centerY: 9; radiusX: 3; radiusY: 7; startAngle: 0; sweepAngle: 360; moveToStart: true }
    }
    // Project: a folder.
    ShapePath {
        strokeColor: glyph.icon === "project" ? glyph.tint : "transparent"; strokeWidth: 1.6; fillColor: "transparent"
        joinStyle: ShapePath.RoundJoin; capStyle: ShapePath.RoundCap
        startX: 2; startY: 4
        PathLine { x: 7; y: 4 } PathLine { x: 9; y: 6 } PathLine { x: 16; y: 6 } PathLine { x: 16; y: 15 }
        PathLine { x: 2; y: 15 } PathLine { x: 2; y: 4 }
    }
    // Pin: a pushpin - the cap, the body flaring to its rim, the needle.
    ShapePath {
        strokeColor: glyph.icon === "pin" ? glyph.tint : "transparent"; strokeWidth: 1.6; fillColor: "transparent"
        joinStyle: ShapePath.RoundJoin; capStyle: ShapePath.RoundCap
        startX: 5.5; startY: 2
        PathLine { x: 12.5; y: 2 }
        PathMove { x: 7; y: 2 } PathLine { x: 6.5; y: 7.5 } PathLine { x: 4; y: 10.5 } PathLine { x: 14; y: 10.5 }
        PathLine { x: 11.5; y: 7.5 } PathLine { x: 11; y: 2 }
        PathMove { x: 9; y: 10.5 } PathLine { x: 9; y: 16.5 }
    }
}
