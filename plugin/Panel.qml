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

  // A agenda vem do daemon, que a busca nas contas Google conectadas no Zinom.
  readonly property var upcoming: (stateData && stateData.upcoming_meetings) ? stateData.upcoming_meetings : []
  readonly property string agendaError: (stateData && stateData.agenda_error) ? String(stateData.agenda_error) : ""

  // As notas saem da CLI, que é quem sabe onde o acervo mora.
  property var recentNotes: []

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
    // Reunião de amanhã na barra vira letreiro. Só entra o que é iminente.
    if (nextMeetingSoon) {
      var t = String(nextMeeting.title)
      var curto = t.length > 14 ? t.substring(0, 13) + "…" : t
      return "󰻱  " + formatClock(nextMeeting.start) + " " + curto
    }
    return glyph
  }

  // Trinta minutos: perto o bastante para valer o espaço na barra.
  readonly property int minutosParaProxima: {
    if (!nextMeeting || !nextMeeting.start) return -1
    var ms = parseIso(nextMeeting.start)
    if (ms <= 0) return -1
    return Math.round((ms - nowMs) / 60000)
  }
  readonly property bool nextMeetingSoon: !!(nextMeeting && nextMeeting.title)
    && minutosParaProxima >= -5 && minutosParaProxima <= 30

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
    if (nextMeetingSoon) return "próxima reunião em " + Math.max(0, minutosParaProxima) + " min"
    return "pronto para gravar"
  }

  function run(cmd) { if (root.bar) root.bar.run(cmd) }

  function openPath(path) {
    if (!path) return
    root.run("xdg-open '" + String(path).replace(/'/g, "'\\''") + "'")
    root.close()
  }

  // "hoje 10:42", "ontem 18:03", "02/09 14:00".
  function formatWhen(iso) {
    var ms = parseIso(iso)
    if (ms <= 0) return ""
    var d = new Date(ms)
    var hoje = new Date()
    var mesmoDia = function(a, b) {
      return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
    }
    var ontem = new Date(hoje.getTime() - 86400000)
    var hora = pad(d.getHours()) + ":" + pad(d.getMinutes())
    if (mesmoDia(d, hoje)) return "hoje " + hora
    if (mesmoDia(d, ontem)) return "ontem " + hora
    return pad(d.getDate()) + "/" + pad(d.getMonth() + 1) + " " + hora
  }

  // O aviso do Zinom em português, e não um triângulo sem legenda.
  function zinomLine(result) {
    if (!result) return ""
    var z = result.zinom
    if (!z) return ""
    if (z.status === "skipped") return "Não enviado ao Zinom: " + (z.reason || "sem motivo declarado")
    if (z.errors && z.errors.length > 0) {
      // "Erro no remember: HTTP 406..." é linguagem de log, não de produto.
      var motivo = String(z.errors[0]).replace(/^Erro no \w+( para .+?)?: /, "")
      if (motivo.length > 60) motivo = motivo.substring(0, 59) + "…"
      return "Não salvou no Zinom (" + motivo + ")"
    }
    var fatos = z.facts_ingested || 0
    if (fatos > 0) return "Salvo no Zinom, com " + fatos + (fatos === 1 ? " fato" : " fatos")
    // O bloco do metadata traz remember_id; o do estado da sessão traz remember.
    if (z.status === "ok" || z.remember || z.remember_id) return "Salvo no Zinom"
    return ""
  }

  function zinomFalhou(result) {
    if (!result || !result.zinom) return false
    var z = result.zinom
    return (z.errors && z.errors.length > 0) || z.status === "error"
  }

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

  Process {
    id: notesProcess
    running: false
    command: ["castanha", "notes", "--json", "--limit", "4"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try {
          var data = JSON.parse(text)
          root.recentNotes = data.notes || []
        } catch (e) {
          root.recentNotes = []
        }
      }
    }
  }

  function refreshNotes() { if (!notesProcess.running) notesProcess.running = true }

  property string syncingSlug: ""

  Process {
    id: syncProcess
    running: false
    onExited: {
      root.syncingSlug = ""
      root.refreshNotes()
    }
  }

  function syncMeeting(slug) {
    if (!slug || syncProcess.running) return
    root.syncingSlug = slug
    syncProcess.command = ["castanha", "sync", slug]
    syncProcess.running = true
  }

  Process {
    id: hideProcess
    running: false
  }

  function hideMeeting(meeting) {
    if (!meeting || hideProcess.running) return
    var chave = meeting.series_key || meeting.uid
    if (!chave) return
    // O estado é reescrito pela própria CLI, e o FileView vê na hora.
    hideProcess.command = ["castanha", "agenda", "hide", String(chave), "--title", String(meeting.title || "")]
    hideProcess.running = true
  }

  onOpenedChanged: if (opened) refreshNotes()

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
      if (root.nextMeetingSoon) return root.nextMeeting.title + " em " + Math.max(0, root.minutosParaProxima) + " min"
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

        // ---------- Reunião em curso ----------
        PanelSeparator {
          width: parent.width
          visible: root.isBusy
          foreground: root.foreground
        }

        Column {
          width: parent.width
          visible: root.isBusy
          spacing: Style.space(4)

          PanelSectionHeader {
            width: parent.width
            text: "REUNIÃO ATUAL"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            text: root.currentMeeting && root.currentMeeting.title ? root.currentMeeting.title : "Gravação avulsa"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            wrapMode: Text.WordWrap
          }
        }

        // ---------- Próximas reuniões ----------
        PanelSeparator { width: parent.width; foreground: root.foreground }

        Column {
          width: parent.width
          spacing: Style.space(4)

          PanelSectionHeader {
            width: parent.width
            text: "PRÓXIMAS REUNIÕES"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            visible: root.upcoming.length === 0
            text: root.agendaError !== "" ? "Agenda indisponível: " + root.agendaError
                                          : "Nada nas próximas horas"
            color: root.agendaError !== "" ? root.urgent : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Repeater {
            model: root.upcoming.slice(0, 4)
            MeetingRow {
              required property var modelData
              width: parent.width
              meeting: modelData
            }
          }
        }

        // ---------- Notas recentes ----------
        PanelSeparator {
          width: parent.width
          visible: root.recentNotes.length > 0
          foreground: root.foreground
        }

        Column {
          width: parent.width
          visible: root.recentNotes.length > 0
          spacing: Style.space(4)

          PanelSectionHeader {
            width: parent.width
            text: "NOTAS RECENTES"
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Repeater {
            model: root.recentNotes
            NoteRow {
              required property var modelData
              width: parent.width
              note: modelData
              // O destino do Zinom só vale para a reunião mais recente: é a
              // única que o estado ainda descreve.
            }
          }
        }
      }
    }
  }

  // ------------------------------------------------------------------ linhas

  // Uma reunião da agenda: hora, título, e de qual conta ela vem. Clicar entra
  // na chamada quando há link, e abre o evento no Google quando não há.
  component MeetingRow: Item {
    id: meetingRow
    property var meeting: null

    readonly property string destino: {
      if (!meeting) return ""
      return meeting.conference_url || meeting.html_link || ""
    }

    implicitHeight: rowCol.implicitHeight + Style.space(8)

    Rectangle {
      anchors.fill: parent
      radius: Style.cornerRadius
      color: rowHover.containsMouse ? root.alpha(root.foreground, 0.06) : "transparent"
    }

    Column {
      id: rowCol
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(6)
      anchors.rightMargin: Style.space(6)
      spacing: Style.space(1)

      Row {
        width: parent.width
        spacing: Style.space(8)

        Text {
          textFormat: Text.PlainText
          text: meetingRow.meeting ? root.formatClock(meetingRow.meeting.start) : ""
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          font.bold: true
          width: Style.space(38)
        }

        Text {
          textFormat: Text.PlainText
          text: meetingRow.meeting && meetingRow.meeting.title ? meetingRow.meeting.title : ""
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
          width: parent.width - Style.space(46) - (chamadaGlyph.visible ? Style.space(18) : 0)
        }

        Text {
          id: chamadaGlyph
          textFormat: Text.PlainText
          visible: !!(meetingRow.meeting && meetingRow.meeting.conference_url) && !rowHover.containsMouse
          text: "󰏌"
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }

        // Só aparece com o mouse em cima: agenda cheia de botão vira ruído.
        PanelActionButton {
          visible: rowHover.containsMouse
          iconText: "󰈉"
          tooltipText: "Não mostrar mais este evento (a série inteira)"
          foreground: root.foreground
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          onClicked: root.hideMeeting(meetingRow.meeting)
        }
      }

      Text {
        textFormat: Text.PlainText
        width: parent.width
        visible: text !== ""
        text: {
          if (!meetingRow.meeting) return ""
          var partes = []
          if (meetingRow.meeting.account) partes.push(meetingRow.meeting.account)
          var n = meetingRow.meeting.attendees ? meetingRow.meeting.attendees.length : 0
          if (n > 0) partes.push(n + (n === 1 ? " participante" : " participantes"))
          return partes.join(" · ")
        }
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
      }
    }

    MouseArea {
      id: rowHover
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: meetingRow.destino !== "" ? Qt.PointingHandCursor : Qt.ArrowCursor
      enabled: meetingRow.destino !== ""
      onClicked: root.openPath(meetingRow.destino)
    }

    PanelToolTip {
      visible: rowHover.containsMouse && meetingRow.destino !== ""
      text: meetingRow.meeting && meetingRow.meeting.conference_url
            ? "Entrar na chamada" : "Abrir o evento no Google Calendar"
      fontFamily: root.fontFamily
    }
  }

  // Uma reunião já gravada: clicar abre as notas em Markdown.
  component NoteRow: Item {
    id: noteRow
    property var note: null

    readonly property bool comProblema: !!(note && note.audio_status && note.audio_status !== "ok"
                                           && note.audio_status !== "desconhecido")

    // O destino do Zinom vem do metadata da própria reunião, e não do estado
    // da sessão: assim toda linha sabe o seu, não só a mais recente.
    readonly property var zinomInfo: note && note.zinom ? note.zinom : null
    readonly property bool precisaSync: !!zinomInfo && zinomInfo.status !== "ok" && zinomInfo.status !== "skipped"
    readonly property bool sincronizando: !!note && root.syncingSlug === note.slug

    implicitHeight: noteCol.implicitHeight + Style.space(8)

    Rectangle {
      anchors.fill: parent
      radius: Style.cornerRadius
      color: noteHover.containsMouse ? root.alpha(root.foreground, 0.06) : "transparent"
    }

    Column {
      id: noteCol
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(6)
      anchors.rightMargin: Style.space(6)
      spacing: Style.space(1)

      Row {
        width: parent.width
        spacing: Style.space(8)

        Text {
          textFormat: Text.PlainText
          text: "󰈙"
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
        }

        Text {
          textFormat: Text.PlainText
          text: noteRow.note && noteRow.note.title ? noteRow.note.title : ""
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
          width: Math.max(0, parent.width - Style.space(24) - quando.implicitWidth - Style.space(8))
        }

        Text {
          id: quando
          textFormat: Text.PlainText
          visible: !sincronizar.visible
          text: noteRow.note ? root.formatWhen(noteRow.note.when) : ""
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
        }

        // Segunda chance para a reunião que não entrou no Zinom.
        PanelActionButton {
          id: sincronizar
          visible: noteRow.precisaSync && (noteHover.containsMouse || noteRow.sincronizando)
          enabled: !noteRow.sincronizando
          iconText: "󰑐"
          tooltipText: noteRow.sincronizando ? "Enviando ao Zinom…" : "Enviar esta reunião ao Zinom de novo"
          foreground: root.foreground
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          onClicked: root.syncMeeting(noteRow.note ? noteRow.note.slug : "")
        }
      }

      Text {
        textFormat: Text.PlainText
        width: parent.width
        visible: noteRow.comProblema
        text: "󰀦  " + (noteRow.note && noteRow.note.audio_diagnostico ? noteRow.note.audio_diagnostico : "")
        color: root.urgent
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }

      Text {
        textFormat: Text.PlainText
        width: parent.width
        visible: text !== ""
        text: {
          if (noteRow.sincronizando) return "󰑐  Enviando ao Zinom…"
          var linha = root.zinomLine({ zinom: noteRow.zinomInfo })
          if (linha === "") return ""
          return (noteRow.precisaSync ? "󰀦  " : "󰧘  ") + linha
        }
        color: noteRow.precisaSync && !noteRow.sincronizando ? root.urgent : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }
    }

    MouseArea {
      id: noteHover
      anchors.fill: parent
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onClicked: root.openPath(noteRow.note ? noteRow.note.silver_path : "")
    }

    PanelToolTip {
      visible: noteHover.containsMouse
      text: "Abrir as notas desta reunião"
      fontFamily: root.fontFamily
    }
  }
}
