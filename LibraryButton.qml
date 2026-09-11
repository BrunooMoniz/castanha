import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Ui as OmarchyUi

Button {
  id: control
  property color foreground: Color.foreground
  property color accent: Color.accent
  property bool selected: false
  font.family: Style.font.family
  font.pixelSize: Style.font.body
  implicitHeight: Math.max(Style.space(34), label.implicitHeight + 2 * Style.spacing.controlPaddingY)
  implicitWidth: label.implicitWidth + 2 * Style.spacing.controlPaddingX + 4
  padding: Style.spacing.controlPaddingX
  hoverEnabled: true
  contentItem: Text {
    id: label
    text: control.text
    textFormat: Text.PlainText
    color: control.selected ? Style.selectedStateColor(control.foreground, control.accent) : control.foreground
    opacity: control.enabled ? 1 : 0.4
    font: control.font
    horizontalAlignment: Text.AlignHCenter
    verticalAlignment: Text.AlignVCenter
    elide: Text.ElideRight
  }
  background: OmarchyUi.BorderSurface {
    objectName: "libraryButtonSurface"
    radius: Style.cornerRadius
    color: control.down ? Style.pressedFillFor(control.foreground, control.accent)
      : control.activeFocus ? Style.focusFillFor(control.foreground, control.accent)
      : control.hovered ? Style.hoverFillFor(control.foreground, control.accent)
      : control.selected ? Style.selectedFillFor(control.foreground, control.accent)
      : Style.normalFillFor(control.foreground, control.accent)
    borderSpec: Border.controlSpec(control.activeFocus ? "focus" : control.hovered ? "hover-cursor" : control.selected ? "selected" : "normal", control.foreground, control.accent)
  }
}
