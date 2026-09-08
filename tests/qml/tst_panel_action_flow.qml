pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Controls
import QtQuick.Window
import QtTest
import "../.." as Castanha
import "../../i18n.js" as I18N

TestCase {
  id: testCase
  name: "PanelActionFlow"
  width: 360
  height: 320
  when: windowShown

  property int hideClicks: 0

  function init() {
    hideClicks = 0
  }

  Window {
    width: 360
    height: 320
    visible: true

    Item {
      id: testSurface
      anchors.fill: parent
    }
  }

  Component {
    id: eventFlowComponent

    Castanha.PanelActionFlow {
      id: eventFlow
      property string locale: "pt"
      width: 342
      spacing: 8

      Button {
        objectName: "join"
        text: I18N.t("btn.join_call", eventFlow.locale)
        width: eventFlow.locale === "pt" ? 122 : 88
      }

      Button {
        objectName: "record"
        text: I18N.t("btn.record_meeting", eventFlow.locale)
        width: 116
      }

      Button {
        objectName: "hidden-event-action"
        visible: false
        width: 500
      }

      Button {
        objectName: "google"
        text: I18N.t("btn.on_google", eventFlow.locale)
        width: 88
      }

      Button {
        objectName: "hide"
        text: I18N.t("btn.hide_from_castanha", eventFlow.locale)
        width: eventFlow.locale === "pt" ? 148 : 140
        onClicked: testCase.hideClicks++
      }
    }
  }

  Component {
    id: noteFlowComponent

    Castanha.PanelActionFlow {
      id: noteFlow
      property string locale: "pt"
      width: 342
      spacing: 8

      Button {
        objectName: "retry"
        text: I18N.t("btn.retry", noteFlow.locale)
        width: noteFlow.locale === "pt" ? 238 : 198
      }

      Button {
        objectName: "notes"
        text: I18N.t("btn.notes", noteFlow.locale)
        width: 72
      }

      Button {
        objectName: "hidden-note-action"
        visible: false
        width: 500
      }

      Button {
        objectName: "transcript"
        text: I18N.t("btn.transcript", noteFlow.locale)
        width: noteFlow.locale === "pt" ? 104 : 96
      }

      Button {
        objectName: "folder"
        text: I18N.t("btn.folder", noteFlow.locale)
        width: 72
      }
    }
  }

  function languageData() {
    return [
      { tag: "pt-BR", locale: "pt" },
      { tag: "english", locale: "en" }
    ]
  }

  function createActionFlow(component, locale) {
    var flow = createTemporaryObject(component, testSurface, { locale: locale })
    verify(flow !== null)
    waitForRendering(flow)
    return flow
  }

  function verifyContained(flow, objectNames) {
    for (var i = 0; i < objectNames.length; i++) {
      var action = findChild(flow, objectNames[i])
      verify(action !== null)
      verify(action.visible)
      verify(action.x >= 0)
      verify(action.x + action.width <= flow.width)
    }
  }

  function test_event_actions_data() {
    return languageData()
  }

  function test_event_actions(data) {
    var flow = createActionFlow(eventFlowComponent, data.locale)
    verifyContained(flow, ["join", "record", "google", "hide"])
    verify(findChild(flow, "hidden-event-action") !== null)
    verify(!findChild(flow, "hidden-event-action").visible)

    var hide = findChild(flow, "hide")
    compare(hide.text, data.locale === "pt" ? "Ocultar do Castanha" : "Hide from Castanha")
    verify(hide.y > findChild(flow, "join").y)
    mouseClick(hide, hide.width / 2, hide.height / 2, Qt.LeftButton)
    compare(testCase.hideClicks, 1)
  }

  function test_note_actions_data() {
    return languageData()
  }

  function test_note_actions(data) {
    var flow = createActionFlow(noteFlowComponent, data.locale)
    verifyContained(flow, ["retry", "notes", "transcript", "folder"])
    verify(findChild(flow, "hidden-note-action") !== null)
    verify(!findChild(flow, "hidden-note-action").visible)
    verify(findChild(flow, "folder").y > findChild(flow, "retry").y)
  }
}
