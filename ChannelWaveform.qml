import QtQuick

// Histórico de níveis medidos. Não há movimento aleatório ou áudio simulado.
Item {
    id: root
    property real peak: 0
    property bool active: false
    property color ink: palette.highlight
    property int barCount: 26
    property var samples: []
    SystemPalette { id: palette }
    implicitHeight: 30
    function reset() { samples = [] }
    onActiveChanged: if (!active) reset()
    Timer {
        interval: 120
        running: root.active
        repeat: true
        onTriggered: {
            var next = root.samples.slice(-(root.barCount - 1))
            next.push(isFinite(root.peak) ? Math.max(0, Math.min(1, root.peak)) : 0)
            root.samples = next
        }
    }
    Row {
        anchors.fill: parent
        spacing: 3
        Repeater {
            model: root.barCount
            Rectangle {
                required property int index
                readonly property int offset: root.barCount - root.samples.length
                readonly property real value: root.active && index >= offset ? (root.samples[index - offset] || 0) : 0
                width: Math.max(1, (root.width - (root.barCount - 1) * 3) / root.barCount)
                height: Math.max(2, Math.sqrt(value) * root.height)
                anchors.verticalCenter: parent.verticalCenter
                radius: width / 2
                color: root.ink
                opacity: value > 0.005 ? 0.9 : 0.23
                Behavior on height { NumberAnimation { duration: 110; easing.type: Easing.OutCubic } }
                Behavior on opacity { NumberAnimation { duration: 110 } }
            }
        }
    }
}
