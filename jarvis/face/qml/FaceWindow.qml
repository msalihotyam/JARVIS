import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// FACESHELL1: the new PC face as a clickable empty shell. Sample data only, no backend:
// every control changes visual state only. One window, three sizes: ring / small / large.
Window {
    id: root
    objectName: "faceWindow"
    title: "JARVIS"
    visible: true
    color: mode === "ring" ? "transparent" : "#11161f"
    flags: mode === "ring" ? (Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint) : Qt.Window

    property string mode: "small"                 // ring | small | large
    property string replyMode: "voice"            // voice | text
    property string placeholder: ""
    readonly property var sizes: ({ ring: [96, 96], small: [420, 560], large: [960, 640] })
    readonly property var turns: [
        { kind: "user", who: qsTr("Sen"), text: "Kasım faturamdaki fazla ücreti bul, itiraz taslağı hazırla." },
        { kind: "understood", who: qsTr("Anladığım"),
          text: qsTr("Anladığım: %1").arg("Kasım faturasındaki fazla ücreti bulup operatöre bir itiraz taslağı hazırlamamı istiyorsun.") },
        { kind: "jarvis", who: "JARVIS", text: "Faturada 89,90 TL'lik bir ek paket var. Taslak sağdaki panelde." },
        { kind: "phone", who: qsTr("Telefon"), text: "Faturanın ekran görüntüsünü telefondan gönderdim." },
        { kind: "jarvis", who: "JARVIS", text: "Gördüm, tutar aynı. Göndermeden önce sana soracağım." }
    ]

    // Fit a size to the screen: the window never grows past what is available (room left for the frame).
    function fit(want, avail, frame) { return Math.max(64, Math.min(want, avail - frame)) }
    function setMode(m) {
        mode = m
        width = fit(sizes[m][0], Math.min(Screen.width, Screen.desktopAvailableWidth), m === "ring" ? 0 : 16)
        height = fit(sizes[m][1], Math.min(Screen.height, Screen.desktopAvailableHeight), m === "ring" ? 0 : 48)
        if (x + width > Screen.virtualX + Screen.desktopAvailableWidth)
            x = Math.max(Screen.virtualX, Screen.virtualX + Screen.desktopAvailableWidth - width - 16)
    }
    function cycleMood() {
        var all = statusRing.moods
        statusRing.mood = all[(all.indexOf(statusRing.mood) + 1) % all.length]
    }
    Component.onCompleted: setMode("small")

    // Ring only: the same ring, big; clicking it opens the small view again.
    StatusRing {
        id: ringOnly
        objectName: "ringOnly"
        visible: root.mode === "ring"
        anchors.fill: parent; anchors.margins: 8
        mood: statusRing.mood
        onClicked: root.setMode("small")
    }

    RowLayout {
        visible: root.mode !== "ring"
        anchors.fill: parent
        spacing: 0

        ColumnLayout {                                    // the small view (left pane when large)
            objectName: "smallPane"
            Layout.fillHeight: true
            Layout.fillWidth: root.mode !== "large"
            Layout.preferredWidth: root.mode === "large" ? 420 : -1
            Layout.margins: 12
            spacing: 10

            RowLayout {                                   // top: ring + focus label + demo + grow
                Layout.fillWidth: true
                spacing: 8
                StatusRing {
                    id: statusRing
                    objectName: "statusRing"
                    Layout.preferredWidth: 44; Layout.preferredHeight: 44
                    onClicked: root.setMode("ring")
                }
                ColumnLayout {
                    Layout.fillWidth: true; Layout.preferredWidth: 100
                    spacing: 0
                    Text { text: qsTr("ODAK"); color: "#7f8da3"; font.pixelSize: 10; font.letterSpacing: 1.2 }
                    Text {
                        objectName: "focusLabel"
                        Layout.fillWidth: true; elide: Text.ElideRight
                        text: "Telefon faturası itirazı"; color: "#e6edf7"; font.pixelSize: 15; font.bold: true
                    }
                }
                Button {
                    objectName: "stateDemo"
                    text: qsTr("Durum: %1").arg(statusRing.label)
                    Layout.preferredWidth: 118
                    Accessible.name: qsTr("Demo: halka durumunu değiştir")
                    onClicked: root.cycleMood()
                }
                Button {
                    objectName: "growButton"
                    text: root.mode === "large" ? qsTr("Küçült") : qsTr("Büyüt")
                    Layout.preferredWidth: 64
                    onClicked: root.setMode(root.mode === "large" ? "small" : "large")
                }
            }

            ListView {                                    // sample turns
                objectName: "turns"
                Layout.fillWidth: true; Layout.fillHeight: true
                clip: true; spacing: 8
                model: root.turns
                delegate: Rectangle {
                    required property var modelData
                    width: ListView.view.width
                    height: body.implicitHeight + tag.implicitHeight + 18
                    radius: 8
                    color: modelData.kind === "user" ? "#1d2a3d"
                         : modelData.kind === "understood" ? "#1f1d33"
                         : modelData.kind === "phone" ? "#1b2b26" : "#171e29"
                    border.color: modelData.kind === "understood" ? "#7c8cff" : "transparent"
                    Text {
                        id: tag
                        x: 10; y: 6
                        text: modelData.who
                        color: modelData.kind === "phone" ? "#38d39f" : modelData.kind === "understood" ? "#a9b4ff" : "#7f8da3"
                        font.pixelSize: 11; font.bold: true
                    }
                    Text {
                        id: body
                        x: 10; y: tag.y + tag.implicitHeight + 3; width: parent.width - 20
                        text: modelData.text; wrapMode: Text.Wrap
                        color: "#e6edf7"; font.pixelSize: 13
                        font.italic: modelData.kind === "understood"
                    }
                }
            }

            Text {                                        // attach placeholder notice
                objectName: "placeholder"
                visible: root.placeholder !== ""
                Layout.fillWidth: true; wrapMode: Text.Wrap
                text: root.placeholder; color: "#ffb347"; font.pixelSize: 12
                Timer { running: parent.visible; interval: 3000; onTriggered: root.placeholder = "" }
            }

            RowLayout {                                   // bottom row: talk, text, attach, reply mode
                objectName: "bottomRow"
                Layout.fillWidth: true
                spacing: 6
                Button {
                    objectName: "pttButton"
                    text: pressed ? qsTr("Dinliyor…") : qsTr("Bas konuş")
                    Layout.preferredWidth: 84
                    Accessible.name: qsTr("Basılı tut ve konuş")
                    onPressedChanged: statusRing.mood = pressed ? "listening" : "understanding"
                }
                TextField {
                    objectName: "textInput"
                    Layout.fillWidth: true; Layout.minimumWidth: 60
                    placeholderText: qsTr("Yaz…")
                    onAccepted: { text = ""; statusRing.mood = "thinking" }
                }
                Button {
                    id: attachButton
                    objectName: "attachButton"
                    text: "+"
                    Layout.preferredWidth: 36
                    Accessible.name: qsTr("Ekle")
                    onClicked: attachMenu.open()
                    Menu {
                        id: attachMenu
                        objectName: "attachMenu"
                        y: -implicitHeight
                        MenuItem { objectName: "attachPaste"; text: qsTr("Yapıştır"); onTriggered: root.placeholder = qsTr("%1: bu kabukta yalnız yer tutucu").arg(text) }
                        MenuItem { objectName: "attachFile"; text: qsTr("Dosya"); onTriggered: root.placeholder = qsTr("%1: bu kabukta yalnız yer tutucu").arg(text) }
                        MenuItem { objectName: "attachShot"; text: qsTr("Ekran görüntüsü"); onTriggered: root.placeholder = qsTr("%1: bu kabukta yalnız yer tutucu").arg(text) }
                    }
                }
                Row {                                     // reply mode switch: Sesli | Yazılı
                    objectName: "replyMode"
                    Button {
                        objectName: "replyVoice"; text: qsTr("Sesli"); width: 56
                        checkable: true; checked: root.replyMode === "voice"
                        onClicked: root.replyMode = "voice"
                    }
                    Button {
                        objectName: "replyText"; text: qsTr("Yazılı"); width: 60
                        checkable: true; checked: root.replyMode === "text"
                        onClicked: root.replyMode = "text"
                    }
                }
            }
        }

        Rectangle {                                       // large only: the content pane on the right
            objectName: "contentPane"
            visible: root.mode === "large"
            Layout.fillWidth: true; Layout.fillHeight: true
            color: "#0d1219"
            Column {
                anchors.fill: parent; anchors.margins: 16
                spacing: 10
                Text { text: qsTr("İÇERİK"); color: "#7f8da3"; font.pixelSize: 10; font.letterSpacing: 1.2 }
                Text { text: "İtiraz taslağı (örnek)"; color: "#e6edf7"; font.pixelSize: 16; font.bold: true }
                Text {
                    width: parent.width; wrapMode: Text.Wrap; color: "#c3cede"; font.pixelSize: 13
                    text: "Merhaba,\n\nKasım 2026 faturamda talep etmediğim 89,90 TL tutarında bir ek paket ücreti yer alıyor. Bu ücretin iptalini ve iadesini rica ederim.\n\nSaygılarımla"
                }
                Rectangle {
                    width: parent.width; height: 120; radius: 6; color: "#171e29"; border.color: "#2a3546"
                    Text { anchors.centerIn: parent; text: qsTr("Ekran görüntüsü: fatura_kasim.png (örnek)"); color: "#7f8da3"; font.pixelSize: 12 }
                }
            }
        }
    }
}
