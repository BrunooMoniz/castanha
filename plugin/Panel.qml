// Castanha na barra do Omarchy.
//
// Regra da casa, e é o ponto do arquivo: nenhuma cor literal. Tudo sai de
// Color.* e Style.*, que o omarchy-shell re-avalia quando o tema troca. Um
// "#ff5555" aqui vira uma mancha vermelha no catppuccin-latte e some no
// vantablack. Os glifos são os mesmos Material Design que o resto do shell
// usa (󰻂 é o glifo de gravação do próprio Omarchy).

import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import Quickshell.Services.Pipewire
import qs.Commons
import qs.Ui

Panel {
  id: root
  moduleName: "moniz.castanha"
  ipcTarget: "castanha"

  // ----------------------------------------------------------------- tema
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  // Esmaecer por alfa, não por Qt.darker: escurecer some no tema claro.
  readonly property color dim: alpha(foreground, 0.55)
  readonly property color faint: alpha(foreground, 0.05)
  readonly property color track: Style.selectedFillFor(foreground, Color.accent, urgent)

  function alpha(c, a) { return Qt.rgba(c.r, c.g, c.b, a) }

  // ---------------------------------------------------------------- estado
  property var stateData: ({})

  readonly property string status: stateData && stateData.status ? stateData.status : "idle"
  readonly property bool isRecording: status === "recording"
  readonly property bool isPaused: status === "paused"
  readonly property bool isProcessing: status === "processing"
  readonly property bool isBusy: isRecording || isPaused

  readonly property var currentMeeting: stateData ? stateData.current_meeting : null
  readonly property var nextMeeting: stateData ? stateData.next_meeting : null
  readonly property var lastResult: stateData ? stateData.last_result : null
  readonly property var meeting: isBusy ? currentMeeting : nextMeeting

  readonly property string mode: stateData && stateData.mode ? stateData.mode : "dual"
  readonly property string modeLabel: mode === "mic_only" ? "somente microfone" : "microfone + chamada"

  // O cronômetro vem do started_at, não do elapsed_seconds do estado: aquele
  // campo só anda quando o daemon está de pé, e a barra não pode depender disso.
  property double nowMs: Date.now()
  readonly property double startedMs: parseIso(stateData ? stateData.started_at : "")
  readonly property int elapsedSeconds: {
    if (!isBusy || startedMs <= 0) return 0
    return Math.max(0, Math.floor((nowMs - startedMs) / 1000))
  }

  function parseIso(text) {
    var s = String(text || "")
    if (s === "") return 0
    // O Python escreve microssegundos; o Date do QML quer milissegundos.
    s = s.replace(/(\.\d{3})\d+/, "$1")
    var ms = Date.parse(s)
    return isFinite(ms) ? ms : 0
  }

  function pad(n) { return (n < 10 ? "0" : "") + n }

  function formatTime(sec) {
    var m = Math.floor(sec / 60), s = sec % 60, h = Math.floor(m / 60)
    m = m % 60
    return h > 0 ? pad(h) + ":" + pad(m) + ":" + pad(s) : pad(m) + ":" + pad(s)
  }

  function formatClock(iso) {
    var ms = parseIso(iso)
    if (ms <= 0) return ""
    var d = new Date(ms)
    return pad(d.getHours()) + ":" + pad(d.getMinutes())
  }

  function formatDuration(sec) {
    var n = Math.round(Number(sec) || 0)
    if (n <= 0) return ""
    if (n < 60) return n + "s"
    var m = Math.floor(n / 60)
    return m + " min"
  }

  // --------------------------------------------------------- microfone vivo
  // O teste de 04/09 gravou 35 minutos de silêncio porque o mic estava mudo no
  // teclado. O painel passa a dizer isso antes, não depois.
  readonly property var micSource: Pipewire.defaultAudioSource
  readonly property bool micMuted: micSource && micSource.audio ? micSource.audio.muted : false

  PwObjectTracker { objects: root.micSource ? [root.micSource] : [] }

  // ----------------------------------------------------------- diagnósticos
  readonly property string lastAudioStatus: lastResult && lastResult.audio_status ? lastResult.audio_status : "ok"
  readonly property bool lastHadProblem: lastAudioStatus !== "ok" && lastAudioStatus !== "desconhecido"
  readonly property var lastZinom: lastResult ? lastResult.zinom : null
  readonly property int lastZinomErrors: lastZinom && lastZinom.errors ? lastZinom.errors.length : 0

  // ------------------------------------------------------------ apresentação
  readonly property string glyph: {
    if (isRecording) return "󰻂"
    if (isPaused) return "󰏤"
    if (isProcessing) return "󰔟"
    if (micMuted) return "󰍭"
    // O microfone é a identidade do Castanha na barra. Não colide com
    // omarchy.microphone: aquele widget não está no layout, e mesmo que
    // estivesse, o estado mudo dos dois é a mesma verdade.
    return "󰍬"
  }

  readonly property string barText: {
    if (isRecording || isPaused) return glyph + "  " + formatTime(elapsedSeconds)
    if (isProcessing) return glyph + "  salvando"
    if (nextMeeting && nextMeeting.title) {
      var t = String(nextMeeting.title)
      var hora = formatClock(nextMeeting.start)
      var curto = t.length > 18 ? t.substring(0, 17) + "…" : t
      return "󰻱  " + (hora ? hora + " " : "") + curto
    }
    return glyph
  }

  readonly property string statusLabel: {
    if (isRecording) return "gravando"
    if (isPaused) return "pausado"
    if (isProcessing) return "salvando"
    return "ocioso"
  }

  readonly property string heroMeta: {
    if (isRecording || isPaused) return formatTime(elapsedSeconds) + " · " + modeLabel
    if (isProcessing) return "transcrevendo e escrevendo as notas"
    if (micMuted) return "microfone mudo no sistema"
    return "pronto para gravar"
  }

  function run(cmd) { if (root.bar) root.bar.run(cmd) }

  function toggleRecording() {
    run(root.isBusy ? "castanha stop" : "castanha start")
    root.close()
  }

  // ------------------------------------------------------------------ dados
  FileView {
    id: stateFile
    path: Quickshell.env("HOME") + "/.local/state/castanha/state.json"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: {
      try {
        var txt = text()
        if (txt) root.stateData = JSON.parse(txt)
      } catch (e) {}
    }
  }

  // Só corre enquanto há o que contar.
  Timer {
    interval: 1000
    running: root.isBusy || root.opened
    repeat: true
    onTriggered: {
      root.nowMs = Date.now()
      if (root.opened) stateFile.reload()
    }
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // ------------------------------------------------------------ botão da barra
  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.barText
    active: root.isRecording
    tooltipText: {
      if (root.isRecording) return "Gravando há " + root.formatTime(root.elapsedSeconds) + " · clique para o painel, direito para finalizar"
      if (root.isPaused) return "Gravação pausada em " + root.formatTime(root.elapsedSeconds)
      if (root.isProcessing) return "Processando as notas da reunião"
      if (root.micMuted) return "Castanha · o microfone está mudo"
      return "Castanha · clique para o painel, direito para gravar"
    }

    onPressed: function(btn) {
      if (btn === Qt.RightButton) root.run("castanha toggle")
      else root.toggle()
    }
  }

  // ------------------------------------------------------------------ painel
  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(340))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(560))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent

      onActivateRequested: root.toggleRecording()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: column
        width: parent.width
        spacing: Style.space(12)

        // ---------- Hero ----------
        PanelHero {
          width: parent.width
          title: "Castanha"
          meta: root.heroMeta
          detail: root.isBusy || root.isProcessing ? root.statusLabel.toUpperCase() : ""
          foreground: root.foreground
          fontFamily: root.fontFamily

          iconComponent: Component {
            Item {
              width: Style.font.display
              height: Style.font.display

              Text {
                anchors.centerIn: parent
                textFormat: Text.PlainText
                text: root.glyph
                color: root.isRecording || root.micMuted ? root.urgent : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.display

                // Pulsa só enquanto grava: é o sinal de que está vivo.
                SequentialAnimation on opacity {
                  running: root.isRecording
                  loops: Animation.Infinite
                  NumberAnimation { to: 0.35; duration: 900; easing.type: Easing.InOutSine }
                  NumberAnimation { to: 1.0; duration: 900; easing.type: Easing.InOutSine }
                  onRunningChanged: if (!running) parent.opacity = 1.0
                }
              }
            }
          }
        }

        // ---------- Aviso de microfone mudo ----------
        Item {
          width: parent.width
          visible: root.micMuted
          implicitHeight: visible ? micWarn.implicitHeight + Style.space(16) : 0

          Rectangle {
            anchors.fill: parent
            radius: Style.cornerRadius
            color: root.alpha(root.urgent, 0.12)
          }

          Row {
            id: micWarn
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: Style.space(10)
            anchors.rightMargin: Style.space(10)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(8)

            Text {
              textFormat: Text.PlainText
              text: "󰍭"
              color: root.urgent
              font.family: root.fontFamily
              font.pixelSize: Style.font.icon
              anchors.verticalCenter: parent.verticalCenter
            }

            Text {
              textFormat: Text.PlainText
              width: micWarn.width - Style.space(28)
              text: "Microfone mudo. A gravação sai em silêncio até você desmutar."
              color: root.urgent
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
              anchors.verticalCenter: parent.verticalCenter
            }
          }
        }

        // ---------- Ação principal ----------
        Button {
          width: parent.width
          text: root.isBusy ? "Finalizar e salvar" : "Iniciar gravação"
          iconText: root.isBusy ? "󰓛" : "󰻂"
          enabled: !root.isProcessing
          active: root.isBusy
          foreground: root.foreground
          fontFamily: root.fontFamily
          bordered: true
          onClicked: root.toggleRecording()
        }

        // ---------- Ações secundárias ----------
        Row {
          width: parent.width
          spacing: Style.space(18)
          visible: root.isBusy || !!(root.meeting && root.meeting.conference_url)

          PanelActionButton {
            visible: root.isBusy
            iconText: root.isPaused ? "󰐊" : "󰏤"
            tooltipText: root.isPaused ? "Retomar a gravação" : "Pausar a gravação"
            foreground: root.foreground
            fontFamily: root.fontFamily
            onClicked: root.run(root.isPaused ? "castanha resume" : "castanha pause")
          }

          PanelActionButton {
            visible: !!(root.meeting && root.meeting.conference_url)
            iconText: "󰏌"
            tooltipText: "Entrar na chamada"
            foreground: root.foreground
            fontFamily: root.fontFamily
            onClicked: {
              if (root.meeting && root.meeting.conference_url)
                root.run("xdg-open '" + root.meeting.conference_url + "'")
              root.close()
            }
          }

          PanelActionButton {
            iconText: "󰉋"
            tooltipText: "Abrir a pasta de notas"
            foreground: root.foreground
            fontFamily: root.fontFamily
            onClicked: { root.run("castanha notes --open"); root.close() }
          }
        }

        // ---------- Reunião ----------
        PanelSeparator { width: parent.width; foreground: root.foreground }

        Column {
          width: parent.width
          spacing: Style.space(6)

          PanelSectionHeader {
            width: parent.width
            text: root.isBusy ? "REUNIÃO ATUAL" : "PRÓXIMA REUNIÃO"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            text: root.meeting && root.meeting.title ? root.meeting.title : "Nenhuma reunião no calendário"
            color: root.meeting && root.meeting.title ? root.foreground : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            wrapMode: Text.WordWrap
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            visible: text !== ""
            text: {
              if (!root.meeting) return ""
              var partes = []
              var hora = root.formatClock(root.meeting.start)
              if (hora) partes.push(hora)
              var convidados = root.meeting.attendees ? root.meeting.attendees.length : 0
              if (convidados > 0) partes.push(convidados + (convidados === 1 ? " participante" : " participantes"))
              return partes.join(" · ")
            }
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }

        // ---------- Última reunião ----------
        PanelSeparator {
          width: parent.width
          visible: !!root.lastResult
          foreground: root.foreground
        }

        Column {
          width: parent.width
          visible: !!root.lastResult
          spacing: Style.space(6)

          PanelSectionHeader {
            width: parent.width
            text: "ÚLTIMA REUNIÃO"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Item {
            width: parent.width
            implicitHeight: Math.max(lastTitle.implicitHeight, lastMeta.implicitHeight)

            Text {
              id: lastTitle
              textFormat: Text.PlainText
              text: root.lastResult && root.lastResult.title ? root.lastResult.title : ""
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              elide: Text.ElideRight
              anchors.left: parent.left
              anchors.right: lastMeta.left
              anchors.rightMargin: Style.space(8)
              anchors.verticalCenter: parent.verticalCenter
            }

            Text {
              id: lastMeta
              textFormat: Text.PlainText
              text: root.lastZinomErrors > 0 ? "󰀦 Zinom" : (root.lastResult && root.lastResult.zinom && root.lastResult.zinom.facts_ingested > 0 ? "󰧘 " + root.lastResult.zinom.facts_ingested : "")
              visible: text !== ""
              color: root.lastZinomErrors > 0 ? root.urgent : root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
            }
          }

          // O diagnóstico do áudio só aparece quando há o que dizer.
          Text {
            textFormat: Text.PlainText
            width: parent.width
            visible: root.lastHadProblem
            text: "󰀦  " + (root.lastResult && root.lastResult.audio_diagnostico ? root.lastResult.audio_diagnostico : "")
            color: root.urgent
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }
        }
      }
    }
  }
}
