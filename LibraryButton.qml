import QtQuick
import QtQuick.Controls
import qs.Commons

Button {
  id: control
  property color foreground: Color.foreground
  property color accent: Color.accent
  property bool selected: false
  implicitHeight: 38
  implicitWidth: label.implicitWidth + 24
  padding: 10
  hoverEnabled: true
  contentItem: Text {
    id: label
    text: control.text
    textFormat: Text.PlainText
    color: control.foreground
    opacity: control.enabled ? 1 : 0.4
    font: control.font
    horizontalAlignment: Text.AlignHCenter
    verticalAlignment: Text.AlignVCenter
    elide: Text.ElideRight
  }
  background: Rectangle {
    radius: 7
    color: Qt.rgba(control.foreground.r, control.foreground.g, control.foreground.b,
                   control.down ? 0.18 : control.selected ? 0.12 : control.hovered ? 0.08 : 0.035)
    border.width: control.activeFocus || control.selected ? 1 : 0
    border.color: control.accent
  }
}
