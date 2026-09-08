import QtQuick
import Quickshell
import qs.Ui
import "i18n.js" as I18N

ShellRoot {
  id: root

  property int hideClicks: 0

  function contained(flow) {
    for (var i = 0; i < flow.children.length; ++i) {
      var action = flow.children[i]
      if (!action.visible) continue
      if (action.x < 0 || action.x + action.width > flow.width) return false
      if (action.y < 0 || action.y + action.height > flow.implicitHeight) return false
    }
    return true
  }

  FloatingWindow {
    visible: true
    implicitWidth: 360
    implicitHeight: 420

    Column {
      spacing: 12

      PanelActionFlow {
        id: eventPt
        width: 342
        spacing: 8
        topPadding: 3

        Button { text: I18N.t("btn.join_call", "pt"); iconText: "󰏌"; bordered: true }
        Button { text: I18N.t("btn.record_meeting", "pt"); iconText: "󰻂" }
        Button { visible: false; width: 500 }
        Button { text: I18N.t("btn.on_google", "pt"); iconText: "󰏌" }
        Button {
          id: hidePt
          text: I18N.t("btn.hide_from_castanha", "pt")
          iconText: "󰈉"
          onClicked: root.hideClicks++
        }
      }

      PanelActionFlow {
        id: eventEn
        width: 342
        spacing: 8
        topPadding: 3

        Button { text: I18N.t("btn.join_call", "en"); iconText: "󰏌"; bordered: true }
        Button { text: I18N.t("btn.record_meeting", "en"); iconText: "󰻂" }
        Button { text: I18N.t("btn.on_google", "en"); iconText: "󰏌" }
        Button {
          id: hideEn
          text: I18N.t("btn.hide_from_castanha", "en")
          iconText: "󰈉"
          onClicked: root.hideClicks++
        }
      }

      PanelActionFlow {
        id: notePt
        width: 342
        spacing: 8
        topPadding: 4

        Button { text: I18N.t("btn.retry", "pt"); iconText: "󰑐"; bordered: true }
        Button { text: I18N.t("btn.notes", "pt"); iconText: "󰈙"; bordered: true }
        Button { text: I18N.t("btn.transcript", "pt"); iconText: "󰗊"; bordered: true }
        Button { text: I18N.t("btn.folder", "pt"); iconText: "󰉋" }
      }

      PanelActionFlow {
        id: noteEn
        width: 342
        spacing: 8
        topPadding: 4

        Button { text: I18N.t("btn.retry", "en"); iconText: "󰑐"; bordered: true }
        Button { text: I18N.t("btn.notes", "en"); iconText: "󰈙"; bordered: true }
        Button { text: I18N.t("btn.transcript", "en"); iconText: "󰗊"; bordered: true }
        Button { text: I18N.t("btn.folder", "en"); iconText: "󰉋" }
      }
    }
  }

  Timer {
    interval: 500
    running: true
    onTriggered: {
      eventPt.forceLayout()
      eventEn.forceLayout()
      notePt.forceLayout()
      noteEn.forceLayout()
      hidePt.clicked()
      hideEn.clicked()

      var ok = root.hideClicks === 2
            && root.contained(eventPt) && root.contained(eventEn)
            && root.contained(notePt) && root.contained(noteEn)
      console.info(ok ? "CASTANHA_REAL_LAYOUT_OK" : "CASTANHA_REAL_LAYOUT_FAIL")
      Qt.quit()
    }
  }
}
