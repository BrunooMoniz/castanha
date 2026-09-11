import QtQuick
import qs.Commons
import "StageStatus.js" as StageStatus

Row {
    id: root
    property var note: ({})
    property color foreground: Color.foreground
    property string fontFamily: Style.font.family
    spacing: Style.space(4)
    Repeater {
        model: StageStatus.stages(root.note)
        Rectangle {
            required property var modelData
            width: (root.width - 3 * root.spacing) / 4
            height: Style.space(46)
            radius: Style.cornerRadius
            color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, modelData.state === "done" ? 0.1 : 0.035)
            Column {
                anchors.centerIn: parent
                spacing: 3
                Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    text: modelData.state === "done" ? "✓" : modelData.state === "error" ? "!" : modelData.state === "removed" ? "−" : "·"
                    color: modelData.state === "error" ? Color.urgent : root.foreground
                    font.pixelSize: Style.font.body
                }
                Text {
                    text: modelData.label
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                }
            }
        }
    }
}
