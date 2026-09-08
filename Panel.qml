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
import "DeliveryStatus.js" as DeliveryStatus
import "i18n.js" as I18N

Panel {
  id: root
  moduleName: "io.github.brunoomoniz.castanha"
  ipcTarget: "castanha"

  // Inglês é o default; pt só quando o locale do sistema é português.
  readonly property string lang: Qt.locale().name.startsWith("pt") ? "pt" : "en"

  // ----------------------------------------------------------------- tema
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  // Esmaecer por alfa, não por Qt.darker: escurecer some no tema claro.
  readonly property color dim: alpha(foreground, 0.55)
  readonly property color faint: alpha(foreground, 0.05)
  readonly property color track: Style.selectedFillFor(foreground, Color.accent, urgent)

  function alpha(c, a) { return Qt.rgba(c.r, c.g, c.b, a) }
  // A cor "urgente" de vários temas é um vermelho escuro (#a55555 no padrão):
  // sobre o fundo escuro do painel o microfone mudo quase sumia. Em tema
  // escuro a cor de estado clareia; em tema claro, escurece.
  readonly property bool darkPanel: (0.2126 * Color.popups.background.r + 0.7152 * Color.popups.background.g
                                     + 0.0722 * Color.popups.background.b) < 0.5
  function contrasting(c) { return darkPanel ? Qt.lighter(c, 1.45) : Qt.darker(c, 1.25) }

  // ---------------------------------------------------------------- estado
  property var stateData: ({})

  readonly property string status: stateData && stateData.status ? stateData.status : "idle"
  readonly property bool isRecording: status === "recording"
  readonly property bool isPaused: status === "paused"
  readonly property bool isProcessing: status === "processing"
  readonly property bool isBusy: isRecording || isPaused

  readonly property var currentMeeting: stateData ? stateData.current_meeting : null
  readonly property var nextMeeting: stateData ? stateData.next_meeting : null
  readonly property var lastResult: DeliveryStatus.projectedLastResult(stateData ? stateData.last_result : null, recentNotes)
  readonly property var meeting: isBusy ? currentMeeting : nextMeeting

  // A agenda vem do daemon, que a busca nas contas Google conectadas no Zinom.
  readonly property var upcoming: (stateData && stateData.upcoming_meetings) ? stateData.upcoming_meetings : []
  readonly property string agendaError: (stateData && stateData.agenda_error) ? String(stateData.agenda_error) : ""

  // As notas saem da CLI, que é quem sabe onde o acervo mora.
  property var recentNotes: []

  // Uma reunião aberta por vez. Vazio = nenhuma.
  property string expandedUid: ""

  function toggleExpanded(uid) {
    root.expandedUid = (root.expandedUid === uid) ? "" : String(uid || "")
  }

  property string expandedNoteSlug: ""

  function toggleExpandedNote(slug) {
    root.expandedNoteSlug = (root.expandedNoteSlug === slug) ? "" : String(slug || "")
  }


  readonly property string mode: stateData && stateData.mode ? stateData.mode : "dual"
  readonly property string modeLabel: mode === "mic_only" ? I18N.t("mode.mic_only", lang) : I18N.t("mode.dual", lang)

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
    if (isProcessing) return glyph + "  " + I18N.t("status.saving", lang)
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
    if (isRecording) return I18N.t("status.recording", lang)
    if (isPaused) return I18N.t("status.paused", lang)
    if (isProcessing) return I18N.t("status.saving", lang)
    return I18N.t("status.idle", lang)
  }

  readonly property string heroMeta: {
    if (isRecording || isPaused) return formatTime(elapsedSeconds) + " · " + modeLabel
    if (isProcessing) return I18N.t("hero.processing", lang)
    if (micMuted) return I18N.t("hero.mic_muted", lang)
    if (nextMeetingSoon) return I18N.t("hero.next_meeting", lang).replace("{n}", Math.max(0, minutosParaProxima))
    return I18N.t("hero.ready", lang)
  }

  function run(cmd) { if (root.bar) root.bar.run(cmd) }

  function openPath(path) {
    if (!path) return
    root.run("xdg-open '" + String(path).replace(/'/g, "'\\''") + "'")
    root.close()
  }

  // Como o Google chama, e como se diz em português.
  function respostaLabel(resposta) {
    if (resposta === "accepted") return I18N.t("rsvp.accepted", lang)
    if (resposta === "declined") return I18N.t("rsvp.declined", lang)
    if (resposta === "tentative") return I18N.t("rsvp.tentative", lang)
    if (resposta === "needsAction") return I18N.t("rsvp.needs_action", lang)
    return ""
  }

  function respostaGlyph(resposta) {
    if (resposta === "accepted") return "󰄬"
    if (resposta === "declined") return "󰅖"
    if (resposta === "tentative") return "󰔟"
    // Círculo vazio para quem ainda não respondeu: a caixa do close-box-outline
    // não desenha nesta fonte e saía como quadrado.
    return "󰄰"
  }

  function intervalo(m) {
    if (!m) return ""
    if (m.all_day) return I18N.t("meeting.all_day", lang)
    var ini = formatClock(m.start)
    var fim = formatClock(m.end)
    return fim && fim !== ini ? I18N.t("meeting.time_range", lang).replace("{start}", ini).replace("{end}", fim) : ini
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
    if (mesmoDia(d, hoje)) return I18N.t("when.today", lang).replace("{t}", hora)
    if (mesmoDia(d, ontem)) return I18N.t("when.yesterday", lang).replace("{t}", hora)
    return pad(d.getDate()) + "/" + pad(d.getMonth() + 1) + " " + hora
  }

  // O aviso do Zinom no idioma do painel, e não um triângulo sem legenda.
  function zinomLine(result) { return DeliveryStatus.zinomLine(result, root.lang) }

  function zinomFalhou(result) {
    if (!result || !result.zinom) return false
    var z = result.zinom
    return (z.errors && z.errors.length > 0) || z.status === "error"
  }

  // O diagnóstico chega persistido em português; o enum audio_status é que
  // permite localizar. Sem enum conhecido, mostra a string que veio.
  function audioDiag(note) {
    var st = note && note.audio_status ? String(note.audio_status) : ""
    if (st !== "" && I18N.STRINGS["audio_status." + st]) return I18N.t("audio_status." + st, lang)
    return note && note.audio_diagnostico ? String(note.audio_diagnostico) : ""
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
    command: ["castanha", "notes", "--json", "--limit", "6"]
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
    id: deleteRecProcess
    running: false
    onExited: {
      root.refreshNotes()
    }
  }

  function deleteRecording(slug, filename) {
    if (!slug || deleteRecProcess.running) return
    var args = ["castanha", "delete-recording", String(slug)]
    if (filename) args.push(String(filename))
    deleteRecProcess.command = args
    deleteRecProcess.running = true
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

  property string retryingSlug: ""

  Process {
    id: retryProcess
    running: false
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.retryingSlug = ""
        root.refreshNotes()
        stateFile.reload()
      }
    }
    onExited: {
      root.retryingSlug = ""
      root.refreshNotes()
      stateFile.reload()
    }
  }

  function retryMeeting(slug) {
    if (!slug || retryProcess.running) return
    root.retryingSlug = slug
    retryProcess.command = ["castanha", "retry", String(slug), "--json"]
    retryProcess.running = true
  }

  Process {
    id: refreshAgendaProcess
    running: false
    command: ["castanha", "agenda", "refresh", "--json"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        stateFile.reload()
      }
    }
    onExited: {
      stateFile.reload()
    }
  }

  readonly property bool refreshingAgenda: refreshAgendaProcess.running

  function refreshAgenda() {
    if (refreshAgendaProcess.running) return
    refreshAgendaProcess.running = true
  }

  onOpenedChanged: if (opened) {
    stateFile.reload()
    refreshNotes()
  }

  // Relê SEMPRE, e não só com o painel aberto. O estado é escrito de forma
  // atômica (grava .tmp e renomeia por cima), e um observador de arquivo segue
  // o inode antigo depois da primeira troca: sem esta releitura, o painel
  // mostrava a agenda de meia hora atrás, foi o que escondeu o Marco Túlio.
  Timer {
    interval: root.isBusy || root.opened ? 1000 : 10000
    running: true
    repeat: true
    onTriggered: {
      root.nowMs = Date.now()
      stateFile.reload()
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
      if (root.isRecording) return I18N.t("bar.tooltip_recording", root.lang).replace("{t}", root.formatTime(root.elapsedSeconds))
      if (root.isPaused) return I18N.t("bar.tooltip_paused", root.lang).replace("{t}", root.formatTime(root.elapsedSeconds))
      if (root.isProcessing) return I18N.t("bar.tooltip_processing", root.lang)
      if (root.micMuted) return I18N.t("bar.tooltip_mic_muted", root.lang)
      if (root.nextMeetingSoon) return I18N.t("bar.tooltip_next_meeting", root.lang).replace("{title}", root.nextMeeting.title).replace("{n}", Math.max(0, root.minutosParaProxima))
      return I18N.t("bar.tooltip_idle", root.lang)
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
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(panelFlick.contentHeight, Style.space(620))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent

      onActivateRequested: root.toggleRecording()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds

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
              id: heroIcon
              readonly property color ink: root.contrasting(root.isRecording || root.micMuted ? root.urgent : root.foreground)
              width: Style.font.display + Style.space(12)
              height: width

              // Pastilha tingida atrás do ícone: dá fundo próprio ao glifo em qualquer tema.
              Rectangle {
                anchors.fill: parent
                radius: Style.cornerRadius
                color: root.alpha(heroIcon.ink, 0.16)
              }

              Text {
                anchors.centerIn: parent
                textFormat: Text.PlainText
                text: root.glyph
                color: heroIcon.ink
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
            color: root.alpha(root.contrasting(root.urgent), 0.12)
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
              color: root.contrasting(root.urgent)
              font.family: root.fontFamily
              font.pixelSize: Style.font.icon
              anchors.verticalCenter: parent.verticalCenter
            }

            Text {
              textFormat: Text.PlainText
              width: micWarn.width - Style.space(28)
              text: I18N.t("mic_warning.body", root.lang)
              color: root.contrasting(root.urgent)
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
          text: root.isBusy ? I18N.t("btn.finish_save", root.lang) : I18N.t("btn.start_recording", root.lang)
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
            tooltipText: root.isPaused ? I18N.t("btn.resume", root.lang) : I18N.t("btn.pause", root.lang)
            foreground: root.foreground
            fontFamily: root.fontFamily
            onClicked: root.run(root.isPaused ? "castanha resume" : "castanha pause")
          }

          PanelActionButton {
            visible: !!(root.meeting && root.meeting.conference_url)
            iconText: "󰏌"
            tooltipText: I18N.t("btn.join_call", root.lang)
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
            tooltipText: I18N.t("btn.open_notes_folder", root.lang)
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
            text: I18N.t("panel.current_meeting", root.lang)
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            text: root.currentMeeting && root.currentMeeting.title ? root.currentMeeting.title : I18N.t("panel.adhoc_recording", root.lang)
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

          Row {
            width: parent.width
            spacing: Style.space(8)

            PanelSectionHeader {
              text: I18N.t("panel.upcoming_meetings", root.lang)
              foreground: root.foreground
              fontFamily: root.fontFamily
              anchors.verticalCenter: parent.verticalCenter
              width: Math.max(0, parent.width - btnRefreshAgenda.width - Style.space(8))
              elide: Text.ElideRight
            }

            PanelActionButton {
              id: btnRefreshAgenda
              anchors.verticalCenter: parent.verticalCenter
              iconText: "󰑐"
              tooltipText: root.refreshingAgenda ? I18N.t("agenda.refreshing", root.lang) : I18N.t("agenda.refresh_now", root.lang)
              foreground: root.foreground
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              enabled: !root.refreshingAgenda
              opacity: root.refreshingAgenda ? 0.6 : 1.0
              onClicked: root.refreshAgenda()

              RotationAnimation on rotation {
                running: root.refreshingAgenda
                loops: Animation.Infinite
                from: 0
                to: 360
                duration: 900
                onRunningChanged: if (!running) btnRefreshAgenda.rotation = 0
              }
            }
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            visible: root.upcoming.length === 0
            text: root.agendaError !== "" ? root.agendaError
                                          : (root.refreshingAgenda ? I18N.t("agenda.refreshing", root.lang) : I18N.t("agenda.empty", root.lang))
            color: root.agendaError !== "" ? root.urgent : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            visible: root.upcoming.length > 0 && root.agendaError !== ""
            text: "󰀦  " + root.agendaError
            color: root.urgent
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
            text: I18N.t("panel.recent_notes", root.lang)
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
}

  // ------------------------------------------------------------------ linhas

  // Uma reunião da agenda: hora, título, e de qual conta ela vem. Clicar entra
  // na chamada quando há link, e abre o evento no Google quando não há.
  // Uma reunião da agenda. O cabeçalho é sempre visível; clicar abre o detalhe
  // com quem vai, onde é e como entrar, aqui dentro, sem mandar para o navegador.
  component MeetingRow: Column {
    id: meetingRow
    property var meeting: null

    readonly property bool aberta: !!meeting && root.expandedUid === meeting.uid
    readonly property var participantes: (meeting && meeting.attendees) ? meeting.attendees : []

    spacing: 0

    // ---- cabeçalho ------------------------------------------------------
    Item {
      id: cabecalho
      width: meetingRow.width
      implicitHeight: rowCol.implicitHeight + Style.space(8)

      Rectangle {
        anchors.fill: parent
        radius: Style.cornerRadius
        color: rowHover.containsMouse || meetingRow.aberta
               ? root.alpha(root.foreground, 0.06) : "transparent"
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
            text: {
              if (!meetingRow.meeting) return ""
              if (meetingRow.meeting.all_day) return I18N.t("meeting.all_day_short", root.lang)
              return root.formatClock(meetingRow.meeting.start)
            }
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
            // Hora + dois espaçamentos da Row + o slot do botão: antes faltava um
            // espaçamento e o slot era menor que o botão, que saía cortado à direita.
            width: Math.max(0, parent.width - Style.space(38) - 2 * Style.space(8) - acaoEsconder.width)
          }

          // Slot de largura fixa: o ícone de chamada e o botão de esconder se
          // revezam DENTRO dele, então entrar com o mouse não reflui a linha.
          Item {
            width: acaoEsconder.width
            height: acaoEsconder.height
            anchors.verticalCenter: parent.verticalCenter

            Text {
              anchors.centerIn: parent
              textFormat: Text.PlainText
              opacity: !!(meetingRow.meeting && meetingRow.meeting.conference_url) && !rowHover.containsMouse ? 1 : 0
              text: "󰕧"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              Behavior on opacity { NumberAnimation { duration: 120 } }
            }

            PanelActionButton {
              id: acaoEsconder
              anchors.centerIn: parent
              opacity: rowHover.containsMouse ? 1 : 0
              enabled: rowHover.containsMouse
              iconText: "󰈉"
              tooltipText: I18N.t("meeting.hide_series", root.lang)
              foreground: root.foreground
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              onClicked: root.hideMeeting(meetingRow.meeting)
              Behavior on opacity { NumberAnimation { duration: 120 } }
            }
          }
        }

        Text {
          textFormat: Text.PlainText
          width: parent.width
          visible: text !== "" && !meetingRow.aberta
          text: {
            if (!meetingRow.meeting) return ""
            var partes = []
            if (meetingRow.meeting.account) partes.push(meetingRow.meeting.account)
            var n = meetingRow.participantes.length
            if (n > 0) partes.push(I18N.t(n === 1 ? "count.participants_one" : "count.participants_many", root.lang).replace("{n}", n))
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
        cursorShape: Qt.PointingHandCursor
        onClicked: root.toggleExpanded(meetingRow.meeting ? meetingRow.meeting.uid : "")
      }

      PanelToolTip {
        visible: rowHover.containsMouse && !meetingRow.aberta
        text: I18N.t("tooltip.view_meeting_details", root.lang)
        fontFamily: root.fontFamily
      }
    }

    // ---- detalhe --------------------------------------------------------
    Column {
      id: detalhe
      visible: meetingRow.aberta
      width: meetingRow.width - Style.space(18)
      x: Style.space(12)
      topPadding: Style.space(4)
      bottomPadding: Style.space(8)
      spacing: Style.space(5)

      Text {
        textFormat: Text.PlainText
        width: parent.width
        text: root.intervalo(meetingRow.meeting)
              + (meetingRow.meeting && meetingRow.meeting.calendar_name
                 ? "  ·  " + meetingRow.meeting.calendar_name : "")
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }

      Text {
        textFormat: Text.PlainText
        width: parent.width
        visible: !!(meetingRow.meeting && meetingRow.meeting.location)
        text: "󰖎  " + (meetingRow.meeting ? String(meetingRow.meeting.location || "") : "")
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }

      PanelSectionHeader {
        width: parent.width
        visible: meetingRow.participantes.length > 0
        text: meetingRow.participantes.length === 1
              ? I18N.t("panel.participants_one", root.lang)
              : I18N.t("panel.participants_many", root.lang).replace("{n}", meetingRow.participantes.length)
        foreground: root.foreground
        fontFamily: root.fontFamily
      }

      Repeater {
        model: meetingRow.participantes

        Item {
          required property var modelData
          width: detalhe.width
          implicitHeight: pessoaNome.implicitHeight + Style.space(3)

          Text {
            id: pessoaResposta
            textFormat: Text.PlainText
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            width: Style.space(16)
            text: root.respostaGlyph(modelData.response)
            color: modelData.response === "declined" ? root.urgent : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Text {
            id: pessoaEstado
            textFormat: Text.PlainText
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            text: root.respostaLabel(modelData.response)
            color: modelData.response === "declined" ? root.urgent : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Text {
            id: pessoaNome
            textFormat: Text.PlainText
            anchors.left: pessoaResposta.right
            anchors.right: pessoaEstado.left
            anchors.rightMargin: Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
            text: (modelData.name || modelData.email || "")
                  + (modelData.organizer ? I18N.t("attendee.organizer_suffix", root.lang) : "")
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }

          MouseArea {
            id: pessoaHover
            anchors.fill: parent
            hoverEnabled: true
            acceptedButtons: Qt.NoButton
          }

          PanelToolTip {
            visible: pessoaHover.containsMouse && !!modelData.email
            text: modelData.email
            fontFamily: root.fontFamily
          }
        }
      }

      Text {
        textFormat: Text.PlainText
        width: parent.width
        visible: meetingRow.participantes.length === 0
        text: I18N.t("meeting.no_guests", root.lang)
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }

      Row {
        spacing: Style.space(8)
        topPadding: Style.space(3)

        Button {
          visible: !!(meetingRow.meeting && meetingRow.meeting.conference_url)
          text: I18N.t("btn.join_call", root.lang)
          iconText: "󰕧"
          foreground: root.foreground
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          bordered: true
          onClicked: root.openPath(meetingRow.meeting.conference_url)
        }

        Button {
          // Grava este evento, com ou sem link: o uid leva título, participantes
          // e link para a nota; o título vai junto para o caso de a agenda ter
          // mudado desde que o painel abriu.
          text: I18N.t("btn.record_meeting", root.lang)
          iconText: "󰻂"
          foreground: root.foreground
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          bordered: !(meetingRow.meeting && meetingRow.meeting.conference_url)
          enabled: !root.isBusy
          onClicked: {
            var tit = (meetingRow.meeting && meetingRow.meeting.title) ? String(meetingRow.meeting.title) : I18N.t("meeting.default_title", root.lang)
            var uid = (meetingRow.meeting && meetingRow.meeting.uid) ? String(meetingRow.meeting.uid) : ""
            var cmd = "castanha start --title='" + tit.replace(/'/g, "'\\''") + "'"
            if (uid) cmd += " --event='" + uid.replace(/'/g, "'\\''") + "'"
            root.run(cmd)
            root.close()
          }
        }

        Button {
          visible: !!(meetingRow.meeting && meetingRow.meeting.html_link)
          text: I18N.t("btn.on_google", root.lang)
          iconText: "󰏌"
          foreground: root.foreground
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          onClicked: root.openPath(meetingRow.meeting.html_link)
        }

        Button {
          text: I18N.t("btn.hide_from_castanha", root.lang)
          iconText: "󰈉"
          foreground: root.foreground
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          onClicked: root.hideMeeting(meetingRow.meeting)
        }
      }
    }
  }

  // Uma reunião já gravada: clicar expande os detalhes com resumo executivo,
  // participantes, gravações de áudio com atalho de exclusão, e links para notas e transcrição.
  component NoteRow: Column {
    id: noteRow
    property var note: null

    readonly property bool aberta: !!note && root.expandedNoteSlug === note.slug
    readonly property bool comProblema: !!(note && note.audio_status && note.audio_status !== "ok"
                                           && note.audio_status !== "desconhecido"
                                           && note.audio_status !== "audio_apagado")

    // O destino do Zinom vem do metadata da própria reunião, e não do estado
    // da sessão: assim toda linha sabe o seu, não só a mais recente.
    readonly property var zinomInfo: note && note.zinom ? note.zinom : null
    // Resumo pendente (cota da LLM) vem do metadata, fora do bloco zinom.
    readonly property var statusInfo: ({ zinom: zinomInfo,
                                         summary_status: note && note.summary_status ? note.summary_status : "",
                                         summary_error: note && note.summary_error ? note.summary_error : "" })
    readonly property bool precisaRetry: !!note && !!note.can_retry
    readonly property bool reprocessando: !!note && root.retryingSlug === note.slug
    readonly property bool precisaSync: DeliveryStatus.zinomNeedsSync(note) && !precisaRetry
    readonly property bool sincronizando: !!note && root.syncingSlug === note.slug

    readonly property var participantes: (note && note.attendees) ? note.attendees : []
    readonly property var gravacoes: (note && note.recordings) ? note.recordings : []

    spacing: 0

    // ---- cabeçalho ------------------------------------------------------
    Item {
      id: noteCabecalho
      width: noteRow.width
      implicitHeight: noteCabecalhoCol.implicitHeight + Style.space(8)

      Rectangle {
        anchors.fill: parent
        radius: Style.cornerRadius
        color: noteHover.containsMouse || noteRow.aberta
               ? root.alpha(root.foreground, 0.06) : "transparent"
      }

      Column {
        id: noteCabecalhoCol
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
            text: noteRow.aberta ? "󰅀" : "󰈙"
            color: noteRow.aberta ? root.foreground : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
          }

          Text {
            textFormat: Text.PlainText
            text: noteRow.note && noteRow.note.title ? noteRow.note.title : ""
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            font.bold: noteRow.aberta
            elide: Text.ElideRight
            width: Math.max(0, parent.width - Style.space(24) - 2 * Style.space(8)
                            - Math.max(quando.implicitWidth, Style.space(20)))
          }

          Item {
            width: Math.max(quando.implicitWidth, Style.space(20))
            height: Math.max(quando.implicitHeight, Style.space(20))
            anchors.verticalCenter: parent.verticalCenter

            Text {
              id: quando
              anchors.centerIn: parent
              textFormat: Text.PlainText
              opacity: (sincronizar.opacity > 0 || reprocessar.opacity > 0) ? 0 : 1
              text: noteRow.note ? root.formatWhen(noteRow.note.when) : ""
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              Behavior on opacity { NumberAnimation { duration: 120 } }
            }

            // Segunda chance para reprocessar upload / transcrição que falhou
            PanelActionButton {
              id: reprocessar
              anchors.centerIn: parent
              opacity: noteRow.precisaRetry && (noteHover.containsMouse || noteRow.reprocessando) ? 1 : 0
              enabled: opacity > 0 && !noteRow.reprocessando
              iconText: "󰑐"
              tooltipText: noteRow.reprocessando ? I18N.t("tooltip.reprocessing", root.lang) : I18N.t("tooltip.retry_upload", root.lang)
              foreground: root.foreground
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              onClicked: root.retryMeeting(noteRow.note ? noteRow.note.slug : "")
              Behavior on opacity { NumberAnimation { duration: 120 } }
            }

            // Segunda chance para a reunião que não entrou no Zinom.
            PanelActionButton {
              id: sincronizar
              anchors.centerIn: parent
              opacity: noteRow.precisaSync && (noteHover.containsMouse || noteRow.sincronizando) ? 1 : 0
              enabled: opacity > 0 && !noteRow.sincronizando
              iconText: "󰑐"
              tooltipText: noteRow.sincronizando ? I18N.t("zinom.sending", root.lang) : I18N.t("zinom.resend", root.lang)
              foreground: root.foreground
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              onClicked: root.syncMeeting(noteRow.note ? noteRow.note.slug : "")
              Behavior on opacity { NumberAnimation { duration: 120 } }
            }
          }
        }

        // Subtítulo quando fechado: resumo de duração / status / participantes
        Text {
          textFormat: Text.PlainText
          width: parent.width
          visible: !noteRow.aberta && text !== ""
          text: {
            if (!noteRow.note) return ""
            var partes = []
            if (noteRow.note.duration_seconds > 0)
              partes.push(root.formatDuration(noteRow.note.duration_seconds))
            if (noteRow.participantes.length > 0)
              partes.push(I18N.t(noteRow.participantes.length === 1 ? "count.participants_one" : "count.participants_many", root.lang).replace("{n}", noteRow.participantes.length))
            if (noteRow.note.recordings_count !== undefined) {
              if (noteRow.note.recordings_count === 0) partes.push(I18N.t("note.no_audio", root.lang))
              else if (noteRow.note.recordings_count > 1) partes.push(I18N.t("note.recordings_plural", root.lang).replace("{n}", noteRow.note.recordings_count))
            }
            return partes.join(" · ")
          }
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }

        Text {
          textFormat: Text.PlainText
          width: parent.width
          visible: noteRow.precisaRetry && !noteRow.aberta
          text: noteRow.reprocessando ? "󰑐  " + I18N.t("note.reprocessing", root.lang) : "󰀦  " + I18N.t("note.upload_pending", root.lang)
          color: root.urgent
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }

        Text {
          textFormat: Text.PlainText
          width: parent.width
          visible: noteRow.comProblema && !noteRow.precisaRetry && !noteRow.aberta
          text: "󰀦  " + root.audioDiag(noteRow.note)
          color: root.urgent
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }

        Text {
          textFormat: Text.PlainText
          width: parent.width
          visible: text !== "" && !noteRow.aberta
          text: {
            if (noteRow.sincronizando) return "󰑐  " + I18N.t("zinom.sending", root.lang)
            var linha = root.zinomLine(noteRow.statusInfo)
            if (linha === "") return ""
            return DeliveryStatus.zinomIcon(noteRow.note) + linha
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
        onClicked: root.toggleExpandedNote(noteRow.note ? noteRow.note.slug : "")
      }

      PanelToolTip {
        visible: noteHover.containsMouse
        text: noteRow.aberta ? I18N.t("note.collapse_details", root.lang) : I18N.t("note.view_details", root.lang)
        fontFamily: root.fontFamily
      }
    }

    // ---- detalhe expandido ----------------------------------------------
    Column {
      id: noteDetalhe
      visible: noteRow.aberta
      width: noteRow.width - Style.space(18)
      x: Style.space(12)
      topPadding: Style.space(4)
      bottomPadding: Style.space(10)
      spacing: Style.space(6)

      // Meta: data, duração e modo
      Text {
        textFormat: Text.PlainText
        width: parent.width
        text: {
          if (!noteRow.note) return ""
          var partes = []
          if (noteRow.note.when) partes.push(root.formatWhen(noteRow.note.when))
          if (noteRow.note.duration_seconds > 0) partes.push(root.formatDuration(noteRow.note.duration_seconds))
            if (noteRow.note.mode) partes.push(I18N.t(noteRow.note.mode === "mic_only" ? "meta.mode_mic" : "meta.mode_call", root.lang))
          return partes.join("  ·  ")
        }
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }

      // Status Zinom
      Text {
        textFormat: Text.PlainText
        width: parent.width
        visible: text !== ""
        text: {
          if (noteRow.sincronizando) return "󰑐  " + I18N.t("zinom.sending", root.lang)
          var linha = root.zinomLine(noteRow.statusInfo)
          if (linha === "") return ""
          return DeliveryStatus.zinomIcon(noteRow.note) + linha
        }
        color: noteRow.precisaSync && !noteRow.sincronizando ? root.urgent : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }

      // Diagnóstico de áudio se houver
      Text {
        textFormat: Text.PlainText
        width: parent.width
        visible: !!(noteRow.note && noteRow.note.audio_diagnostico && noteRow.note.audio_status !== "ok")
        text: "󰀦  " + root.audioDiag(noteRow.note)
        color: noteRow.note && noteRow.note.audio_status === "audio_apagado" ? root.dim : root.urgent
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }

      // Preview do Resumo Executivo
      Column {
        width: parent.width
        visible: !!(noteRow.note && noteRow.note.summary_preview)
        spacing: Style.space(2)

        PanelSectionHeader {
          width: parent.width
          text: I18N.t("panel.summary", root.lang)
          foreground: root.foreground
          fontFamily: root.fontFamily
        }

        Rectangle {
          width: parent.width
          implicitHeight: summaryText.implicitHeight + Style.space(12)
          color: root.alpha(root.foreground, 0.04)
          radius: Style.cornerRadius

          Text {
            id: summaryText
            textFormat: Text.PlainText
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            anchors.margins: Style.space(6)
            text: noteRow.note && noteRow.note.summary_preview ? noteRow.note.summary_preview : ""
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
            maximumLineCount: 5
            elide: Text.ElideRight
          }
        }
      }

      // Participantes
      Column {
        width: parent.width
        visible: noteRow.participantes.length > 0
        spacing: Style.space(3)

        PanelSectionHeader {
          width: parent.width
          text: noteRow.participantes.length === 1
                ? I18N.t("panel.participants_one", root.lang)
                : I18N.t("panel.participants_many", root.lang).replace("{n}", noteRow.participantes.length)
          foreground: root.foreground
          fontFamily: root.fontFamily
        }

        Repeater {
          model: noteRow.participantes

          Item {
            id: pItem
            required property var modelData
            width: noteDetalhe.width
            implicitHeight: pNome.implicitHeight + Style.space(3)

            Text {
              id: pIcon
              textFormat: Text.PlainText
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              width: Style.space(16)
              text: "󰀉"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            Text {
              id: pNome
              textFormat: Text.PlainText
              anchors.left: pIcon.right
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              text: typeof modelData === "string" ? modelData
                    : ((modelData.name || modelData.email || "") + (modelData.organizer ? I18N.t("attendee.organizer_suffix", root.lang) : ""))
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }

            MouseArea {
              id: pHover
              anchors.fill: parent
              hoverEnabled: true
              acceptedButtons: Qt.NoButton
            }

            PanelToolTip {
              visible: pHover.containsMouse && !!(typeof modelData === "object" && modelData.email)
              text: typeof modelData === "object" ? String(modelData.email || "") : ""
              fontFamily: root.fontFamily
            }
          }
        }
      }

      // Gravações de Áudio
      Column {
        width: parent.width
        spacing: Style.space(3)

        PanelSectionHeader {
          width: parent.width
          text: noteRow.gravacoes.length === 1
                ? I18N.t("recordings.header_one", root.lang)
                : (noteRow.gravacoes.length === 0 ? I18N.t("recordings.header_zero", root.lang)
                                                 : I18N.t("recordings.header_many", root.lang).replace("{n}", noteRow.gravacoes.length))
          foreground: root.foreground
          fontFamily: root.fontFamily
        }

        Text {
          textFormat: Text.PlainText
          width: parent.width
          visible: noteRow.gravacoes.length === 0
          text: I18N.t("recordings.deleted_notice", root.lang)
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.WordWrap
        }

        Repeater {
          model: noteRow.gravacoes

          Item {
            id: recItem
            required property var modelData
            width: noteDetalhe.width
            implicitHeight: Math.max(Style.space(24), recFilename.implicitHeight + Style.space(6))

            Rectangle {
              anchors.fill: parent
              radius: Style.cornerRadius
              color: recHover.containsMouse ? root.alpha(root.foreground, 0.05) : "transparent"
            }

            Text {
              id: recIcon
              textFormat: Text.PlainText
              anchors.left: parent.left
              anchors.leftMargin: Style.space(4)
              anchors.verticalCenter: parent.verticalCenter
              text: "󰓃"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            Text {
              id: recFilename
              textFormat: Text.PlainText
              anchors.left: recIcon.right
              anchors.leftMargin: Style.space(6)
              anchors.right: recSize.left
              anchors.rightMargin: Style.space(6)
              anchors.verticalCenter: parent.verticalCenter
              text: modelData.filename || "audio.ogg"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideMiddle
            }

            Text {
              id: recSize
              textFormat: Text.PlainText
              anchors.right: recPlayBtn.left
              anchors.rightMargin: Style.space(4)
              anchors.verticalCenter: parent.verticalCenter
              text: modelData.size_human || ""
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            PanelActionButton {
              id: recPlayBtn
              anchors.right: recDelBtn.left
              anchors.rightMargin: Style.space(2)
              anchors.verticalCenter: parent.verticalCenter
              iconText: "󰐊"
              tooltipText: I18N.t("btn.play_recording", root.lang)
              foreground: root.foreground
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              onClicked: root.openPath(modelData.path)
            }

            PanelActionButton {
              id: recDelBtn
              anchors.right: parent.right
              anchors.rightMargin: Style.space(4)
              anchors.verticalCenter: parent.verticalCenter
              iconText: "󰆴"
              tooltipText: I18N.t("btn.delete_recording", root.lang)
              foreground: root.urgent
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              enabled: !deleteRecProcess.running
              onClicked: root.deleteRecording(noteRow.note ? noteRow.note.slug : "", modelData.filename)
            }

            MouseArea {
              id: recHover
              anchors.fill: parent
              hoverEnabled: true
              acceptedButtons: Qt.NoButton
            }
          }
        }
      }

      // Botões de ação principais (Notas, Transcrição, Pasta)
      Row {
        spacing: Style.space(8)
        topPadding: Style.space(4)

        Button {
          visible: !!(noteRow.note && noteRow.note.can_retry)
          text: noteRow.reprocessando ? I18N.t("btn.reprocessing", root.lang) : I18N.t("btn.retry", root.lang)
          iconText: "󰑐"
          foreground: root.foreground
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          bordered: true
          enabled: !root.retryingSlug
          onClicked: root.retryMeeting(noteRow.note ? noteRow.note.slug : "")
        }

        Button {
          text: I18N.t("btn.notes", root.lang)
          iconText: "󰈙"
          foreground: root.foreground
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          bordered: true
          onClicked: root.openPath(noteRow.note ? noteRow.note.silver_path : "")
        }

        Button {
          visible: !!(noteRow.note && noteRow.note.has_transcript)
          text: I18N.t("btn.transcript", root.lang)
          iconText: "󰗊"
          foreground: root.foreground
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          bordered: true
          onClicked: root.openPath(noteRow.note ? noteRow.note.transcript_path : "")
        }

        Button {
          visible: !!(noteRow.note && noteRow.note.bronze_dir)
          text: I18N.t("btn.folder", root.lang)
          iconText: "󰉋"
          foreground: root.foreground
          fontFamily: root.fontFamily
          fontSize: Style.font.caption
          onClicked: root.openPath(noteRow.note ? noteRow.note.bronze_dir : "")
        }
      }
    }
  }
}
