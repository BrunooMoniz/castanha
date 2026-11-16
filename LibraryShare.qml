// Compartilhar as notas da reunião com os agentes da config (`agents`). Sem agente, não ocupa espaço.
// O pedido vai pelo CLI (`castanha agents share`); o estado (`agents`) pertence a quem instancia.
import QtQuick
import QtQuick.Layouts
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui as OmarchyUi
import "LibraryLogic.js" as LibraryLogic

ColumnLayout {
  id: root
  property var agents: []
  property string meetingSlug: ""
  property bool hasNotes: false
  property var cliCommand: ["castanha"]
  property color foreground: Commons.Color.popups.text
  property color background: Commons.Color.popups.background
  property color accent: Commons.Color.accent
  property string fontFamily: Style.font.family
  readonly property color muted: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.66)
  // Agente com o painel de confirmação aberto e o público escolhido nele.
  property string openAgent: ""
  property string audience: ""
  property var messages: ({})
  readonly property bool busy: shareProcess.running
  signal changed()
  visible: agents.length > 0 && meetingSlug !== ""
  spacing: Style.space(6)

  function message(slug, text) { var next = Object.assign({}, messages); next[slug] = text; messages = next }
  function open(agent) {
    openAgent = agent.id
    audience = agent.audiences.length ? agent.audiences[0].id : ""
  }
  function send(agent, remove) {
    if (!meetingSlug || busy) return
    var args = ["agents", "share", "--json"]
    // Valor colado à opção: um público que comece com "-" não vira flag.
    if (remove) args.push("--remove"); else if (audience) args.push("--audience=" + audience)
    shareProcess.requestedSlug = meetingSlug
    shareProcess.command = cliCommand.concat(args, ["--", meetingSlug, agent.id])
    message(meetingSlug, remove ? "Deixando de compartilhar…" : "Compartilhando com " + agent.name + "…")
    openAgent = ""
    shareProcess.running = true
  }
  onMeetingSlugChanged: openAgent = ""

  Process {
    id: shareProcess
    objectName: "libraryShareProcess"
    property string requestedSlug: ""
    stdout: StdioCollector {}
    onExited: function(code) {
      var text
      try {
        var result = JSON.parse(stdout.text)
        // Só a recusa explícita traz texto confiável: saída com erro e "ok" no corpo não vira mensagem de sucesso.
        if (code !== 0 || ["ok", "pending"].indexOf(result.status) < 0) throw new Error(result.status === "error" && result.message ? result.message : "")
        text = result.message || "Pronto."
      } catch (error) { text = String(error.message || "") || "Não foi possível falar com o agente. Nada mudou." }
      root.message(requestedSlug, text)
      root.changed()
    }
  }

  Repeater {
    model: root.agents
    ColumnLayout {
      id: agentRow
      required property var modelData
      readonly property var state: LibraryLogic.shareState(modelData, root.meetingSlug)
      Layout.fillWidth: true
      spacing: Style.space(6)
      RowLayout {
        Layout.fillWidth: true
        spacing: Style.space(8)
        Text {
          objectName: "libraryShareLine"
          Layout.fillWidth: true
          text: LibraryLogic.shareLine(agentRow.modelData, agentRow.state, root.hasNotes)
          textFormat: Text.PlainText; wrapMode: Text.Wrap
          color: agentRow.state ? root.foreground : root.muted
          font.family: root.fontFamily; font.pixelSize: Style.font.body
        }
        LibraryButton {
          objectName: "libraryShareButton"
          // Reunião que o agente recebe por regra dele: aqui só se mostra, não se desfaz.
          visible: !agentRow.modelData.error && !(agentRow.state && agentRow.state.auto)
          text: root.openAgent === agentRow.modelData.id ? "Cancelar" : agentRow.state ? "Deixar de compartilhar" : "Compartilhar com " + agentRow.modelData.name + "…"
          enabled: !root.busy && (!!agentRow.state || root.hasNotes)
          foreground: root.foreground; accent: root.accent
          font.family: root.fontFamily; font.pixelSize: Style.font.body
          onClicked: root.openAgent === agentRow.modelData.id ? root.openAgent = "" : agentRow.state ? root.send(agentRow.modelData, true) : root.open(agentRow.modelData)
        }
      }
      ColumnLayout {
        visible: root.openAgent === agentRow.modelData.id
        Layout.fillWidth: true
        spacing: Style.space(6)
        Text {
          Layout.fillWidth: true
          text: agentRow.modelData.name + " passa a ler o resumo e a transcrição desta reunião. O áudio não é enviado."
            + (agentRow.modelData.audiences.length > 1 ? " Escolha quem pode perguntar sobre ela:" : "")
          textFormat: Text.PlainText; wrapMode: Text.Wrap
          color: root.foreground; font.family: root.fontFamily; font.pixelSize: Style.font.body
        }
        OmarchyUi.Dropdown {
          objectName: "libraryShareAudience"
          Layout.fillWidth: true
          visible: agentRow.modelData.audiences.length > 1
          showLabel: false
          options: agentRow.modelData.audiences.map(function(a) { return {value: a.id, label: a.label} })
          value: root.audience
          foreground: root.foreground; background: root.background; accent: root.accent; fontFamily: root.fontFamily
          onChanged: function(value) { root.audience = String(value) }
        }
        LibraryButton {
          objectName: "libraryShareConfirm"
          text: "Compartilhar"
          enabled: !root.busy
          foreground: root.foreground; accent: root.accent; font.family: root.fontFamily
          onClicked: root.send(agentRow.modelData, false)
        }
      }
    }
  }
  Text {
    objectName: "libraryShareFeedback"
    Layout.fillWidth: true
    visible: !!root.messages[root.meetingSlug]
    text: root.messages[root.meetingSlug] || ""
    textFormat: Text.PlainText; wrapMode: Text.Wrap
    color: root.foreground; font.family: root.fontFamily; font.pixelSize: Style.font.body
  }
}
