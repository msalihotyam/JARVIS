.pragma library
// Gate 6G K1: the one drawn icon set (Icon.qml draws it). Every icon is SVG
// path data on an 18-unit grid, stroked 1.6 with round caps and joins - the
// SummaryIcon language - and scaled to the requested size, so it stays crisp
// from 14 to 24 px and never depends on a font. A string is a stroke; an
// object may add `f`, a filled path in the same tint. "missing" is the
// visible fallback for an unknown name.

var circle = function (cx, cy, r) {
    return "M" + (cx - r) + " " + cy + " A" + r + " " + r + " 0 1 0 " + (cx + r) + " " + cy
         + " A" + r + " " + r + " 0 1 0 " + (cx - r) + " " + cy
}
var rect = function (x1, y1, x2, y2) {
    return "M" + x1 + " " + y1 + " L" + x2 + " " + y1 + " L" + x2 + " " + y2 + " L" + x1 + " " + y2 + " Z"
}

var paths = {
    // The summary and sidebar icons (formerly SummaryIcon's own paths).
    "tasks": "M5 3 L3 3 L3 17 L15 17 L15 3 L13 3 M6 1.5 L12 1.5 L12 4.5 L6 4.5 Z M6 9 L12 9 M6 13 L12 13",
    "updates": "M1.5 4 L16.5 4 L16.5 14.5 L1.5 14.5 L1.5 4 L9 10 L16.5 4",
    "focus": { s: circle(9, 9.5, 7) + " M9 0.5 L9 3", f: circle(9, 9.5, 2.2) },
    "plan": "M3.5 1.5 L14.5 1.5 L14.5 16.5 L9 12.5 L3.5 16.5 Z M9 4.8 L9 8.6 M7.1 6.7 L10.9 6.7",
    "memory": "M2.5 4 A6.5 2.5 0 1 0 15.5 4 A6.5 2.5 0 1 0 2.5 4 M2.5 4 L2.5 14 M15.5 4 L15.5 14"
              + " M15.5 9 A6.5 2.5 0 0 1 2.5 9 M15.5 14 A6.5 2.5 0 0 1 2.5 14",
    "global": circle(9, 9, 7) + " M2 9 L16 9 M6 9 A3 7 0 1 0 12 9 A3 7 0 1 0 6 9",
    "project": "M2 4 L7 4 L9 6 L16 6 L16 15 L2 15 Z",
    "folder": "M2 4 L7 4 L9 6 L16 6 L16 15 L2 15 Z",
    "pin": "M5.5 2 L12.5 2 M7 2 L6.5 7.5 L4 10.5 L14 10.5 L11.5 7.5 L11 2 M9 10.5 L9 16.5",

    // Pages.
    "home": "M2.5 8.5 L9 2.5 L15.5 8.5 M4 7.2 L4 15.5 L14 15.5 L14 7.2 M7.5 15.5 L7.5 11 L10.5 11 L10.5 15.5",
    "history": circle(9, 9, 7) + " M9 5 L9 9 L12 10.8",
    "clock": circle(9, 9, 7) + " M9 5 L9 9 L12 10.8",
    "automations": "M15.75 9 A6.75 6.75 0 1 1 9 2.25 C10.9 2.25 12.7 3 14.05 4.3 L15.75 6 M15.75 2.25 L15.75 6 L12 6",

    // Controls.
    "search": circle(7.5, 7.5, 5) + " M11.2 11.2 L16 16",
    "plus": "M9 3 L9 15 M3 9 L15 9",
    "minus": "M3.5 9 L14.5 9",
    "close": "M4 4 L14 14 M14 4 L4 14",
    "check": "M3.5 9.5 L7.5 13.5 L14.5 4.5",
    "more": { s: "", f: circle(3.5, 9, 1.4) + " " + circle(9, 9, 1.4) + " " + circle(14.5, 9, 1.4) },
    "chevron-left": "M11 3.5 L5.5 9 L11 14.5",
    "chevron-right": "M7 3.5 L12.5 9 L7 14.5",
    "chevron-down": "M3.5 6.5 L9 12 L14.5 6.5",
    "chevron-up": "M3.5 11.5 L9 6 L14.5 11.5",
    "back": "M15 9 L3 9 M8 4 L3 9 L8 14",
    "forward": "M3 9 L15 9 M10 4 L15 9 L10 14",
    "arrow-up": "M9 15 L9 3 M4 8 L9 3 L14 8",
    "arrow-down": "M9 3 L9 15 M4 10 L9 15 L14 10",
    "arrow-up-right": "M4.5 13.5 L13.5 4.5 M6.5 4.5 L13.5 4.5 L13.5 11.5",
    "send": "M9 15 L9 3 M4 8 L9 3 L14 8",
    "stop": { s: rect(4.5, 4.5, 13.5, 13.5), f: rect(4.5, 4.5, 13.5, 13.5) },
    "pause": { s: rect(5, 3.5, 7.2, 14.5) + " " + rect(10.8, 3.5, 13, 14.5),
               f: rect(5, 3.5, 7.2, 14.5) + " " + rect(10.8, 3.5, 13, 14.5) },
    "retry": "M15.75 9 A6.75 6.75 0 1 1 9 2.25 C10.9 2.25 12.7 3 14.05 4.3 L15.75 6 M15.75 2.25 L15.75 6 L12 6",
    "reset": "M2.25 9 A6.75 6.75 0 1 0 9 2.25 C7.1 2.25 5.3 3 3.95 4.3 L2.25 6 M2.25 2.25 L2.25 6 L6 6",
    "pencil": "M12 2.5 L15.5 6 L6 15.5 L2.5 15.5 L2.5 12 Z M10 4.5 L13.5 8",
    "trash": "M2.5 4.5 L15.5 4.5 M7 4.5 L7 2.5 L11 2.5 L11 4.5 M4 4.5 L5 16 L13 16 L14 4.5 M7.5 7.5 L7.5 13 M10.5 7.5 L10.5 13",
    "archive": rect(2, 3, 16, 6.5) + " M3 6.5 L3 15.5 L15 15.5 L15 6.5 M7 9.5 L11 9.5",
    "copy": rect(6, 6, 15.5, 15.5) + " M12 6 L12 2.5 L2.5 2.5 L2.5 12 L6 12",
    "paperclip": "M14.5 8.5 L8.5 14.5 A3.5 3.5 0 0 1 3.5 9.5 L10 3 A2.3 2.3 0 0 1 13.3 6.3 L7 12.6 A1.05 1.05 0 0 1 5.5 11.1 L11 5.5",
    "quote": "M7 3.5 L2.5 8 L7 12.5 M2.5 8 L11 8 Q15.5 8 15.5 12.5 L15.5 15",
    "chat": "M3 3.5 L15 3.5 Q16 3.5 16 4.5 L16 11.5 Q16 12.5 15 12.5 L8 12.5 L4.5 15.5 L4.5 12.5 L3 12.5"
            + " Q2 12.5 2 11.5 L2 4.5 Q2 3.5 3 3.5 Z",
    "tiles": rect(2.5, 2.5, 7.5, 7.5) + " " + rect(10.5, 2.5, 15.5, 7.5) + " " + rect(2.5, 10.5, 7.5, 15.5) + " " + rect(10.5, 10.5, 15.5, 15.5),
    "sliders": "M2 5 L9.5 5 M13.5 5 L16 5 M2 13 L4 13 M8 13 L16 13 " + circle(11.5, 5, 2) + " " + circle(6, 13, 2),
    // The two sidebar-hide toggles (6G L4): the chat grows into the space /
    // the space stays empty (a dashed edge).
    "sidebar-grow": rect(2, 3, 16, 15) + " M6.5 3 L6.5 15 M11.8 7 L9.8 9 L11.8 11",
    "sidebar-keep": rect(2, 3, 16, 15) + " M6.5 3 L6.5 4.5 M6.5 7 L6.5 8 M6.5 10 L6.5 11 M6.5 13.5 L6.5 15",
    "hide": circle(9, 9, 7) + " M4.05 13.95 L13.95 4.05",

    // States and markers.
    "warning": { s: "M9 2.2 L16.3 15.3 L1.7 15.3 Z M9 7 L9 10.3", f: circle(9, 12.8, 1) },
    "info": { s: circle(9, 9, 7) + " M9 8.2 L9 12.8", f: circle(9, 5.6, 1) },
    "question": { s: "M6.4 6.5 Q6.4 3.6 9 3.6 Q11.6 3.6 11.6 6.1 Q11.6 7.9 9 9.1 L9 10.8", f: circle(9, 14, 1.1) },
    "exclamation": { s: "M9 3.6 L9 10.8", f: circle(9, 14, 1.1) },
    "dot": { s: "", f: circle(9, 9, 2.4) },
    "diamond": "M9 2 L16 9 L9 16 L2 9 Z",
    "diamond-filled": { s: "M9 3 L15 9 L9 15 L3 9 Z", f: "M9 3 L15 9 L9 15 L3 9 Z" },
    "flag": "M4 16 L4 2.5 M4 3 L14 3 L11.5 6.5 L14 10 L4 10",
    "plug": "M6.5 2 L6.5 5.5 M11.5 2 L11.5 5.5 M3.5 5.5 L14.5 5.5 L14.5 8 A5.5 5.5 0 0 1 3.5 8 Z M9 13.5 L9 16.5",
    "spark": "M9 1.8 Q9.8 8.2 16.2 9 Q9.8 9.8 9 16.2 Q8.2 9.8 1.8 9 Q8.2 8.2 9 1.8 Z",
    "bell": "M4.5 12.5 L4.5 8 A4.5 4.5 0 0 1 13.5 8 L13.5 12.5 L15 14 L3 14 Z M7.3 16.3 L10.7 16.3 M9 1.8 L9 3.5",
    "grip": { s: "", f: circle(6, 6, 1.3) + " " + circle(12, 6, 1.3) + " " + circle(6, 12, 1.3) + " " + circle(12, 12, 1.3) },

    // Window controls.
    "minimize": "M4.5 9 L13.5 9",
    "maximize": rect(4, 4, 14, 14),
    "restore": rect(3.5, 7, 11, 14.5) + " M7 7 L7 3.5 L14.5 3.5 L14.5 11 L11 11",

    // The fallback for an unknown name: a crossed box, never a blank.
    "missing": rect(3, 3, 15, 15) + " M3 3 L15 15 M15 3 L3 15"
}

var names = Object.keys(paths).filter(function (n) { return n !== "missing" })
