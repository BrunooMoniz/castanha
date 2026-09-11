import QtQuick
import "AudioMeter.js" as MeterLogic

// Envelope de volume em tempo real. O perfil das barras é fixo: não simula
// frequência nem deixa fala antiga animando depois que o sinal cai.
Item {
    id: root
    property real peak: 0
    property bool active: false
    property color ink: palette.highlight
    property int barCount: 26
    readonly property real targetLevel: active ? MeterLogic.visualLevel(peak) : 0
    property real displayedLevel: targetLevel
    SystemPalette { id: palette }
    implicitHeight: 30
    Behavior on displayedLevel {
        NumberAnimation { duration: root.targetLevel > root.displayedLevel ? 65 : 160; easing.type: Easing.OutCubic }
    }
    Row {
        anchors.fill: parent
        spacing: 3
        Repeater {
            model: root.barCount
            Rectangle {
                required property int index
                objectName: "levelBar" + index
                readonly property real profile: 0.3 + 0.7 * Math.sin(Math.PI * (index + 0.5) / root.barCount)
                width: Math.max(1, (root.width - (root.barCount - 1) * 3) / root.barCount)
                height: 2 + root.displayedLevel * profile * Math.max(0, root.height - 2)
                anchors.verticalCenter: parent.verticalCenter
                radius: width / 2
                color: root.ink
                opacity: 0.2 + 0.8 * Math.min(1, root.displayedLevel * 3)
            }
        }
    }
}
