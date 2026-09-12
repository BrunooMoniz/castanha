import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell.Io
import qs.Commons
import qs.Ui as OmarchyUi
import "LibraryLogic.js" as LibraryLogic

ColumnLayout {
  id: root
  property string meetingSlug: ""
  property var recordings: []
  property int revision: 0
  property bool canReprocess: recordings.length > 0
  property string contentStatus: ""
  property bool remoteCleanupRequired: false
  property bool restoreAllowed: false
  property string lastExclusionId: ""
  readonly property string undoId: Object.prototype.hasOwnProperty.call(undoIds, meetingSlug) ? undoIds[meetingSlug] : lastExclusionId
  property var cliCommand: ["castanha"]
  property color foreground: Color.popups.text
  property color accent: Color.accent
  property string fontFamily: Style.font.family
  property string armedFile: ""
  // Mover: gravação escolhida, destino ("__new__" = reunião nova) e as reuniões candidatas.
  property string movingFile: ""
  property string moveTarget: ""
  property var moveTargets: []
  property bool moveEnabled: false
  readonly property var moveOptions: [{value: "", label: "Escolha a reunião de destino…"}, {value: "__new__", label: "Nova reunião…"}]
    .concat(moveTargets.map(function(m) { return {value: m.slug, label: m.title + " · " + LibraryLogic.date(m.when)} }))
  readonly property bool canMove: !!movingFile && (moveTarget === "__new__" ? !!moveTitleField.text.trim() : !!moveTarget)
  property var messages: ({})
  property var undoIds: ({})
  readonly property bool busy: actionProcess.running
  readonly property string feedback: messages[meetingSlug] || ""
  signal listen(int index)
  signal beforeMutation()
  signal meetingChanged(string slug)
  signal movedTo(string slug)
  spacing: Style.space(10)

  function message(slug, text) { var next = Object.assign({}, messages); next[slug] = text; messages = next }
  function run(kind, args) {
    if (!meetingSlug || busy) return
    beforeMutation()
    actionProcess.requestedSlug = meetingSlug
    actionProcess.kind = kind
    actionProcess.command = cliCommand.concat(args)
    message(meetingSlug, kind === "retry" ? "Reprocessando a reunião com os áudios atuais…" : kind === "restore" ? "Restaurando gravação…" : kind === "move" ? "Movendo gravação…" : "Excluindo áudio da reunião…")
    armedFile = ""
    cancelMove()
    actionProcess.running = true
  }
  function cancelMove() { movingFile = ""; moveTarget = ""; moveTitleField.text = "" }
  function move() {
    if (!canMove || !recordings.some(function(r) { return r.id === movingFile })) return
    var args = ["recordings", "move", "--json", "--expected-revision", String(revision)]
    // Valor colado à opção: um título que comece com "-" não vira flag.
    args.push(moveTarget === "__new__" ? "--new-title=" + moveTitleField.text.trim() : "--to=" + moveTarget)
    run("move", args.concat(["--", meetingSlug, movingFile]))
  }
  function exclude() {
    if (!armedFile || !recordings.some(function(r) { return r.id === armedFile })) return
    run("exclude", ["recordings", "exclude", "--json", "--expected-revision", String(revision), "--", meetingSlug, armedFile])
  }
  function retry() { if (canReprocess) run("retry", ["retry", "--json", "--", meetingSlug]) }
  function restore() { if (restoreAllowed && undoId) run("restore", ["recordings", "restore", "--json", "--", meetingSlug, undoId]) }
  onMeetingSlugChanged: { armedFile = ""; cancelMove() }

  Process {
    id: actionProcess
    property string requestedSlug: ""
    property string kind: ""
    stdout: StdioCollector {}
    onExited: function(code) {
      try {
        var envelope = JSON.parse(stdout.text)
        var result = kind === "retry" && Array.isArray(envelope.results) ? envelope.results[0] : envelope
        if (!result || code !== 0 || ["ok", "success", "partial", "pending", "empty"].indexOf(result.status) < 0)
          throw new Error(result && result.status === "error" && result.message ? result.message : "Não foi possível concluir a operação. Atualize a reunião e tente novamente.")
        var pending = result.status === "partial" || result.status === "pending" || result.cleanup_status === "pending"
        if (kind === "exclude") {
          var undo = Object.assign({}, root.undoIds)
          undo[requestedSlug] = result.exclusion_id && result.can_restore === true ? result.exclusion_id : ""
          root.undoIds = undo
          root.message(requestedSlug, result.remaining_count > 0
            ? "Áudio excluído. Reprocesse a reunião para atualizar a transcrição, o resumo e os insights."
            : "Áudio excluído. A reunião ficou sem gravações válidas; suas anotações foram preservadas.")
          if (result.cleanup_status === "pending") root.message(requestedSlug, root.messages[requestedSlug] + " A limpeza no Zinom ainda está pendente.")
        } else if (kind === "move") {
          var destination = result.destination && result.destination.slug ? String(result.destination.slug) : ""
          root.message(requestedSlug, (result.message || "Gravação movida.")
            + (result.cleanup_status === "pending" ? " A retirada do conteúdo anterior no Zinom ainda está pendente." : ""))
          if (destination) root.movedTo(destination)
        } else if (kind === "restore") {
          var restored = Object.assign({}, root.undoIds); restored[requestedSlug] = ""; root.undoIds = restored
          root.message(requestedSlug, result.message || "Gravação restaurada. Consulte o estado da reunião antes de reprocessar.")
        } else {
          var data = result.result || {}
          pending = pending || data.transcription_pending || data.summary_status === "pending" || (data.problemas && data.problemas.length > 0) || (data.zinom && ["error", "pending", "pending_cleanup"].indexOf(data.zinom.status) >= 0)
          root.message(requestedSlug, result.status === "empty" ? "A reunião está sem gravações válidas para reprocessar." : pending ? "Processamento retomado. Ainda há etapas pendentes; acompanhe o estado da reunião." : "Reunião reprocessada com os áudios atuais.")
        }
      } catch (error) { root.message(requestedSlug, String(error.message || error)) }
      root.meetingChanged(requestedSlug)
    }
  }

  Repeater {
    model: root.recordings
    delegate: OmarchyUi.BorderSurface {
      required property var modelData
      required property int index
      Layout.fillWidth: true
      implicitHeight: row.implicitHeight + 2 * Style.spacing.controlPaddingY
      radius: Style.cornerRadius
      color: Style.normalFillFor(root.foreground, root.accent)
      borderSpec: Border.controlSpec("normal", root.foreground, root.accent)
      RowLayout {
        id: row
        anchors { left: parent.left; right: parent.right; verticalCenter: parent.verticalCenter; margins: Style.spacing.controlPaddingX }
        spacing: Style.spacing.controlGap
        ColumnLayout {
          Layout.fillWidth: true
          Text { Layout.fillWidth: true; text: "Gravação " + (index + 1); color: root.foreground; font.family: root.fontFamily; font.pixelSize: Style.font.body; textFormat: Text.PlainText }
          Text { Layout.fillWidth: true; text: LibraryLogic.clock(modelData.duration_seconds) + " · " + (modelData.size_bytes / (1024 * 1024)).toFixed(1) + " MB"; color: root.foreground; opacity: 0.7; font.family: root.fontFamily; font.pixelSize: Style.font.caption; textFormat: Text.PlainText }
        }
        LibraryButton { objectName: "recordingListen" + index; text: "Ouvir"; foreground: root.foreground; font.family: root.fontFamily; onClicked: root.listen(index) }
        LibraryButton { objectName: "recordingMove" + index; text: "Mover…"; visible: root.moveEnabled; enabled: !root.busy; foreground: root.foreground; font.family: root.fontFamily; onClicked: { root.armedFile = ""; root.moveTarget = ""; root.movingFile = modelData.id } }
        LibraryButton { objectName: "recordingExclude" + index; text: "Excluir"; enabled: !root.busy; foreground: root.foreground; font.family: root.fontFamily; onClicked: { root.cancelMove(); root.armedFile = modelData.id } }
      }
    }
  }
  ColumnLayout {
    Layout.fillWidth: true
    visible: !!root.movingFile
    spacing: Style.space(6)
    Text { Layout.fillWidth: true; text: "Mover esta gravação para outra reunião? Ela sai desta reunião (uma cópia fica guardada) e a transcrição, o resumo e a entrega ao Zinom das duas reuniões são refeitos automaticamente." + (root.remoteCleanupRequired ? " O conteúdo que esta reunião já enviou ao Zinom será retirado." : ""); textFormat: Text.PlainText; wrapMode: Text.Wrap; color: root.foreground; font.family: root.fontFamily; font.pixelSize: Style.font.body }
    OmarchyUi.Dropdown {
      objectName: "recordingMoveTarget"
      Layout.fillWidth: true
      showLabel: false
      options: root.moveOptions
      value: root.moveTarget
      foreground: root.foreground; accent: root.accent; fontFamily: root.fontFamily
      onChanged: function(value) { root.moveTarget = String(value) }
    }
    TextField {
      id: moveTitleField
      objectName: "recordingMoveTitle"
      Layout.fillWidth: true
      visible: root.moveTarget === "__new__"
      placeholderText: "Título da nova reunião"
      color: root.foreground; font.family: root.fontFamily; selectByMouse: true
      background: OmarchyUi.BorderSurface { color: Style.controlFill(moveTitleField.activeFocus, moveTitleField.hovered, root.foreground, root.accent); radius: Style.cornerRadius; borderSpec: Border.controlSpec(moveTitleField.activeFocus ? "focus" : "normal", root.foreground, root.accent) }
    }
    Flow {
      Layout.fillWidth: true; Layout.preferredHeight: childrenRect.height; spacing: Style.spacing.controlGap
      LibraryButton { objectName: "recordingCancelMove"; text: "Manter aqui"; foreground: root.foreground; onClicked: root.cancelMove() }
      LibraryButton { objectName: "recordingConfirmMove"; text: "Mover gravação"; foreground: root.foreground; enabled: !root.busy && root.canMove; onClicked: root.move() }
    }
  }
  ColumnLayout {
    Layout.fillWidth: true
    visible: !!root.armedFile
    Text { Layout.fillWidth: true; text: "Excluir esta gravação da reunião? Ela deixará de ser usada na transcrição, no resumo e nos insights. " + (root.recordings.length === 1 ? "A reunião ficará sem áudio." : "Depois, reprocesse os áudios restantes.") + " Uma cópia será guardada para recuperação." + (root.remoteCleanupRequired ? " A retirada do conteúdo já enviado ao Zinom não poderá ser desfeita." : ""); textFormat: Text.PlainText; wrapMode: Text.Wrap; color: root.foreground; font.family: root.fontFamily; font.pixelSize: Style.font.body }
    Flow {
      Layout.fillWidth: true; Layout.preferredHeight: childrenRect.height; spacing: Style.spacing.controlGap
      LibraryButton { objectName: "recordingCancelExclude"; text: "Manter gravação"; foreground: root.foreground; onClicked: root.armedFile = "" }
      LibraryButton { objectName: "recordingConfirmExclude"; text: "Excluir da reunião"; foreground: root.foreground; enabled: !root.busy; onClicked: root.exclude() }
    }
  }
  Flow {
    Layout.fillWidth: true; Layout.preferredHeight: childrenRect.height; spacing: Style.spacing.controlGap
    LibraryButton { objectName: "recordingReprocess"; text: actionProcess.running && actionProcess.kind === "retry" ? "Reprocessando…" : "Reprocessar reunião"; foreground: root.foreground; font.family: root.fontFamily; enabled: root.canReprocess && !root.busy; onClicked: root.retry() }
    LibraryButton { objectName: "recordingUndo"; text: "Desfazer exclusão"; foreground: root.foreground; font.family: root.fontFamily; visible: root.restoreAllowed && !!root.undoId; enabled: !root.busy; onClicked: root.restore() }
  }
  Text { Layout.fillWidth: true; visible: !!root.feedback; text: root.feedback; textFormat: Text.PlainText; wrapMode: Text.Wrap; color: root.foreground; font.family: root.fontFamily; font.pixelSize: Style.font.bodySmall }
}
