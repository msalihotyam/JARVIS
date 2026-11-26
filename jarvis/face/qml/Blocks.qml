import QtQuick
import QtQuick.Templates as T

// FACEREAD1: the blocks of one turn. Typed refusal: an unknown "t" or a malformed field draws nothing and throws nothing.
// Salih's blocks always take the plain path, so his text stays literal: no markup, no styling, no links.
// Body text is TextEdit so the mouse can select it; TextEdit has no lineHeight, so lines keep the font's own spacing.
Column {
    id: root
    objectName: "blocks"
    readonly property var tk: face.tokens
    readonly property string mono: "Cascadia Mono"      // no font-family token yet
    property var blocks: []
    property bool salih: false
    property bool collapsed: false                      // only the first block is drawn
    property int hitMin
    readonly property real bodySize: salih ? tk.type_turn_size : tk.type_body_size
    spacing: tk.space_12

    function arr(x) { return x !== null && typeof x === "object" && typeof x.length === "number" ? x : [] }
    function str(x) { return typeof x === "string" ? x : typeof x === "number" && isFinite(x) ? String(x) : "" }
    function spansOf(x) {                               // a block, list item or cell: a string, a span list, {spans} or {text}
        if (typeof x === "string" || typeof x === "number")
            return [{ s: str(x) }]
        if (x === null || typeof x !== "object")
            return []
        if (typeof x.length === "number")
            return x
        if (x.spans !== undefined)
            return arr(x.spans)
        if (x.s !== undefined)
            return [x]
        return typeof x.text === "string" ? [{ s: x.text }] : []
    }
    function span(x) { return x !== null && typeof x === "object" ? x : { s: x } }
    function href(x) { return typeof span(x).href === "string" ? span(x).href : "" }
    function joined(x) {
        var sp = spansOf(x), out = ""
        for (var i = 0; i < sp.length; i++)
            out += str(span(sp[i]).s)
        return out
    }
    function parts(x) {                                 // word pieces when a span is styled, else none (one plain TextEdit)
        var sp = spansOf(x), out = [], rich = false
        if (salih)
            return out
        for (var i = 0; i < sp.length; i++) {
            var s = span(sp[i])
            rich = rich || s.b === true || s.i === true || s.c === true || href(s) !== ""
        }
        for (var j = 0; rich && j < sp.length; j++) {
            var p = span(sp[j])
            var words = str(p.s).match(/\S+\s*|\s+/g) || []
            for (var k = 0; k < words.length; k++)
                out.push({ s: words[k], b: p.b === true, i: p.i === true, c: p.c === true, href: href(p) })
        }
        return out
    }
    function code(b) { return b !== null && typeof b === "object" && typeof b.text === "string" ? b.text : joined(b) }
    function alignOf(a) { return a === "right" ? TextEdit.AlignRight : a === "center" ? TextEdit.AlignHCenter : TextEdit.AlignLeft }
    function columns(head, rows) {
        var n = head.length
        for (var r = 0; r < rows.length; r++)
            n = Math.max(n, arr(rows[r]).length)
        return n
    }
    function widths(head, rows, count) {                // from the text, at most half the column; the cells wrap
        var w = []
        for (var c = 0; c < count; c++) {
            var most = headMetrics.advanceWidth(joined(head[c]).toUpperCase())
            for (var r = 0; r < rows.length; r++)
                most = Math.max(most, cellMetrics.advanceWidth(joined(arr(rows[r])[c])))
            w.push(Math.ceil(Math.min(Math.max(most, tk.space_24), width / 2)))
        }
        return w
    }
    function pick(b) {
        if (b === null || typeof b !== "object")
            return null
        switch (b.t) {
        case "h": return headingBlock
        case "p": return paraBlock
        case "ul": case "ol": return listBlock
        case "table": return tableBlock
        case "code": return codeBlock
        case "note": return noteBlock
        case "rule": return ruleBlock
        }
        return null
    }

    FontMetrics { id: headMetrics; font.pixelSize: root.tk.type_label_size; font.weight: Font.Medium }
    FontMetrics { id: cellMetrics; font.pixelSize: root.tk.type_cell_size }

    Repeater {
        model: root.collapsed ? root.arr(root.blocks).slice(0, 1) : root.arr(root.blocks)
        delegate: Loader {
            required property var modelData
            width: root.width
            visible: item !== null
            sourceComponent: root.pick(modelData)
        }
    }

    component Run: Item {                               // one selectable TextEdit, or word pieces when the spans are styled
        id: run
        readonly property var tk: face.tokens
        property string text
        property var parts: []
        property real size: tk.type_body_size
        property int weight: Font.Normal
        property color tone: tk.color_text1
        property bool caps: false
        property int align: TextEdit.AlignLeft
        property int hitMin
        readonly property bool rich: parts.length > 0
        implicitHeight: rich ? flow.implicitHeight : edit.implicitHeight
        TextEdit {
            id: edit
            objectName: "runText"
            visible: !run.rich
            width: run.width
            text: run.rich ? "" : run.text
            textFormat: TextEdit.PlainText
            readOnly: true
            selectByMouse: true
            wrapMode: TextEdit.WrapAtWordBoundaryOrAnywhere
            horizontalAlignment: run.align
            color: run.tone
            selectionColor: run.tk.color_accent_line
            selectedTextColor: run.tk.color_text1
            font.pixelSize: run.size
            font.weight: run.weight
            font.capitalization: run.caps ? Font.AllUppercase : Font.MixedCase
        }
        Flow {
            id: flow
            objectName: "runFlow"
            visible: run.rich
            width: run.width
            Repeater {
                model: run.parts
                delegate: TextEdit {
                    id: piece
                    required property var modelData
                    readonly property bool link: modelData.href !== ""
                    width: Math.min(implicitWidth, flow.width)  // a word longer than the column wraps inside it
                    text: modelData.s
                    textFormat: TextEdit.PlainText
                    readOnly: true
                    selectByMouse: !link
                    wrapMode: TextEdit.WrapAnywhere
                    color: link ? (area.containsMouse ? run.tk.color_link_hover : run.tk.color_link) : run.tone
                    selectionColor: run.tk.color_accent_line
                    selectedTextColor: run.tk.color_text1
                    font.pixelSize: modelData.c ? run.tk.type_code_size : run.size
                    font.weight: modelData.b ? Font.DemiBold : run.weight
                    font.italic: modelData.i
                    font.underline: link
                    font.family: modelData.c ? "Cascadia Mono" : Qt.application.font.family
                    font.capitalization: run.caps ? Font.AllUppercase : Font.MixedCase
                    Rectangle {
                        visible: piece.modelData.c
                        z: -1
                        anchors.fill: parent
                        radius: run.tk.radius_code / 2
                        color: run.tk.color_surface3
                    }
                    MouseArea {                     // a link only reports its address; the face decides what to open
                        id: area
                        objectName: "linkArea"
                        property string accessibleName: qsTr("Bağlantı: %1").arg(piece.modelData.href)
                        Accessible.role: Accessible.Link
                        Accessible.name: accessibleName
                        visible: piece.link
                        enabled: piece.link
                        anchors.centerIn: parent
                        width: Math.max(run.hitMin, parent.width)
                        height: Math.max(run.hitMin, parent.height)
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: face.linkActivated(piece.modelData.href)
                    }
                }
            }
        }
    }

    component Bullet: Item {                            // a hanging marker in a fixed column, the text beside it
        id: bullet
        property string mark
        property int indent
        property string text
        property var parts: []
        property real size
        property int hitMin
        implicitHeight: body.implicitHeight
        Text {
            objectName: "listMarker"
            text: bullet.mark
            textFormat: Text.PlainText
            color: face.tokens.color_text2
            font.pixelSize: bullet.size
        }
        Run {
            id: body
            x: bullet.indent
            width: bullet.width - bullet.indent
            text: bullet.text
            parts: bullet.parts
            size: bullet.size
            hitMin: bullet.hitMin
        }
    }

    component SideBar: T.ScrollBar {                    // a wide table or code line scrolls sideways inside its own box
        objectName: "sideScroll"
        property int hitMin
        property string accessibleName: qsTr("Yatay kaydırma çubuğu")
        Accessible.name: accessibleName
        policy: T.ScrollBar.AlwaysOn
        visible: size < 1
        height: hitMin
        topPadding: hitMin - face.tokens.space_6 - face.tokens.space_4
        bottomPadding: face.tokens.space_4
        contentItem: Rectangle { implicitHeight: face.tokens.space_6; radius: height / 2; color: face.tokens.color_line2 }
    }

    // each block reads its data from the Loader that holds it; a missing one reads as an empty object
    Component {
        id: headingBlock
        Run {
            objectName: "headingBlock"
            readonly property var block: parent && parent.modelData ? parent.modelData : ({})
            text: root.joined(block)
            parts: root.parts(block)
            size: typeof block.level === "number" && block.level <= 2 ? root.tk.type_h2_size : root.tk.type_h3_size
            weight: Font.DemiBold
            hitMin: root.hitMin
        }
    }
    Component {
        id: paraBlock
        Run {
            objectName: "paraBlock"
            readonly property var block: parent && parent.modelData ? parent.modelData : ({})
            text: root.joined(block)
            parts: root.parts(block)
            size: root.bodySize
            hitMin: root.hitMin
        }
    }
    Component {
        id: listBlock
        Column {
            id: items
            objectName: "listBlock"
            readonly property var block: parent && parent.modelData ? parent.modelData : ({})
            readonly property bool ordered: block.t === "ol"
            readonly property int indent: ordered ? root.tk.space_24 + root.tk.space_8 : root.tk.space_24
            spacing: root.tk.space_6
            Repeater {
                model: root.arr(items.block.items)
                delegate: Column {
                    id: outer
                    required property var modelData
                    required property int index
                    readonly property bool nested: modelData !== null && typeof modelData === "object" && typeof modelData.length !== "number"
                    width: items.width
                    spacing: root.tk.space_6
                    Bullet {
                        width: outer.width
                        indent: items.indent
                        mark: items.ordered ? (outer.index + 1) + "." : "•"
                        text: root.joined(outer.modelData)
                        parts: root.parts(outer.modelData)
                        size: root.bodySize
                        hitMin: root.hitMin
                    }
                    Repeater {                          // the second and last level
                        model: outer.nested ? root.arr(outer.modelData.items) : []
                        delegate: Bullet {
                            required property var modelData
                            required property int index
                            x: items.indent
                            width: outer.width - items.indent
                            indent: items.indent
                            mark: items.ordered ? (index + 1) + "." : "◦"
                            text: root.joined(modelData)
                            parts: root.parts(modelData)
                            size: root.bodySize
                            hitMin: root.hitMin
                        }
                    }
                }
            }
        }
    }
    Component {
        id: tableBlock
        Flickable {
            id: box
            objectName: "tableBox"
            readonly property var block: parent && parent.modelData ? parent.modelData : ({})
            readonly property var head: root.arr(block.head)
            readonly property var rows: root.arr(block.rows)
            readonly property var aligns: root.arr(block.align)
            readonly property int count: root.columns(head, rows)
            readonly property var cols: root.widths(head, rows, count)
            readonly property real full: {
                var sum = 0
                for (var i = 0; i < cols.length; i++)
                    sum += cols[i]
                return sum + root.tk.space_12 * Math.max(0, count - 1)
            }
            interactive: false
            clip: true
            contentWidth: full
            contentHeight: grid.height
            height: grid.height + (contentWidth > width ? root.hitMin : 0)
            T.ScrollBar.horizontal: SideBar { hitMin: root.hitMin }
            Column {
                id: grid
                Row {
                    objectName: "tableHead"
                    spacing: root.tk.space_12
                    bottomPadding: root.tk.space_8
                    Repeater {
                        model: box.count
                        delegate: Run {
                            required property int index
                            width: box.cols[index]
                            text: root.joined(box.head[index])
                            size: root.tk.type_label_size
                            weight: Font.Medium
                            tone: root.tk.color_text3
                            caps: true
                            align: root.alignOf(box.aligns[index])
                            hitMin: root.hitMin
                        }
                    }
                }
                Repeater {
                    model: box.rows
                    delegate: Column {
                        id: line
                        required property var modelData
                        readonly property var cells: root.arr(modelData)
                        Rectangle { objectName: "tableRule"; width: box.full; height: 1; color: root.tk.color_line1 }
                        Row {
                            spacing: root.tk.space_12
                            topPadding: root.tk.space_8
                            bottomPadding: root.tk.space_8
                            Repeater {
                                model: box.count
                                delegate: Run {
                                    required property int index
                                    width: box.cols[index]
                                    text: root.joined(line.cells[index])
                                    parts: root.parts(line.cells[index])
                                    size: root.tk.type_cell_size
                                    align: root.alignOf(box.aligns[index])
                                    hitMin: root.hitMin
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    Component {
        id: codeBlock
        Rectangle {
            id: frame
            objectName: "codeBlock"
            readonly property var block: parent && parent.modelData ? parent.modelData : ({})
            color: root.tk.color_surface3
            radius: root.tk.radius_code
            implicitHeight: scroll.height + root.tk.space_12 * 2
            Flickable {
                id: scroll
                objectName: "codeBox"
                x: root.tk.space_16
                y: root.tk.space_12
                width: frame.width - root.tk.space_16 * 2
                height: codeText.height + (contentWidth > width ? root.hitMin : 0)
                interactive: false
                clip: true
                contentWidth: codeText.width
                contentHeight: codeText.height
                T.ScrollBar.horizontal: SideBar { hitMin: root.hitMin }
                TextEdit {
                    id: codeText
                    objectName: "codeText"
                    text: root.code(frame.block)
                    textFormat: TextEdit.PlainText
                    readOnly: true
                    selectByMouse: true
                    wrapMode: TextEdit.NoWrap
                    color: root.tk.color_text1
                    selectionColor: root.tk.color_accent_line
                    selectedTextColor: root.tk.color_text1
                    font.family: root.mono
                    font.pixelSize: root.tk.type_code_size
                }
            }
        }
    }
    Component {
        id: noteBlock
        Rectangle {
            id: panel
            objectName: "notePanel"
            readonly property var block: parent && parent.modelData ? parent.modelData : ({})
            color: root.tk.color_surface1
            radius: root.tk.radius_panel
            implicitHeight: note.implicitHeight + root.tk.space_14 * 2
            Run {
                id: note
                x: root.tk.space_20
                y: root.tk.space_14
                width: panel.width - root.tk.space_20 * 2
                text: root.joined(panel.block)
                parts: root.parts(panel.block)
                size: root.bodySize
                tone: root.tk.color_text2
                hitMin: root.hitMin
            }
        }
    }
    Component {
        id: ruleBlock
        Rectangle { objectName: "ruleLine"; height: 1; color: root.tk.color_line1 }
    }
}
