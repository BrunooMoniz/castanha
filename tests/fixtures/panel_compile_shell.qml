import QtQuick
import Quickshell
import Quickshell.Wayland
import qs.Commons
import qs.Ui

// Compila o Panel real do Castanha com os componentes reais do shell e
// exercita o menu de contexto e o campo de nome.
//
// As notas vêm da CLI real (`castanha notes --json`), apontada por
// XDG_CONFIG_HOME para um acervo temporário com uma reunião só. Nada de
// injetar recentNotes na mão: o Process do painel sobrescreve isso quando
// responde, e o teste passaria medindo o acervo da máquina.
ShellRoot {
  id: root

  property string lastCommand: ""
  property var falhas: []

  function checar(condicao, mensagem) { if (!condicao) root.falhas.push(mensagem) }

  function terminar() {
    if (root.falhas.length === 0) console.log("CASTANHA_PANEL_OK")
    else console.log("CASTANHA_PANEL_FAIL " + root.falhas.join(" | "))
    Qt.exit(0)
  }

  // A altura natural do conteúdo, depois de o layout assentar.
  function medir(callback) {
    medirTimer.callback = callback
    medirTimer.restart()
  }

  Timer {
    id: medirTimer
    interval: 150
    property var callback: null
    onTriggered: if (callback) callback(castanha.contentNaturalHeight)
  }

  // KeyboardPanel deriva tela e geometria da barra layer-shell, não de uma
  // janela flutuante. O fixture não reserva espaço nem captura input do dono.
  Region { id: noInput; width: 0; height: 0 }
  PanelWindow {
    id: fakeBar
    screen: Quickshell.screens[0]
    anchors { top: true; left: true; right: true }
    implicitHeight: 30
    color: "transparent"
    exclusionMode: ExclusionMode.Ignore
    mask: noInput
    WlrLayershell.namespace: "castanha-panel-test-bar"
    WlrLayershell.layer: WlrLayer.Top
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
    readonly property color foreground: Color.foreground
    readonly property color barForeground: Color.foreground
    readonly property color urgent: Color.urgent
    readonly property string fontFamily: Style.font.family
    readonly property string position: "top"
    readonly property bool vertical: false
    readonly property int barSize: 30
    readonly property bool foregroundAnimationEnabled: false
    property var activePopout: null
    function requestPopout(owner) { activePopout = owner }
    function releasePopout(owner) { if (activePopout === owner) activePopout = null }
    function run(cmd) { root.lastCommand = cmd }
    function switchPanelFrom(panel, direction) {}

    CastanhaPanel {
      id: castanha
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      bar: fakeBar
      manageIpc: false
    }
  }

  // ---- estado puro, antes de mexer em layout -------------------------------
  Timer {
    interval: 300
    running: true
    onTriggered: {
      // O menu de contexto abre, alterna no mesmo slug, e fecha limpando tudo.
      castanha.openContext("qualquer")
      root.checar(castanha.contextSlug === "qualquer", "openContext")
      castanha.openContext("qualquer")
      root.checar(castanha.contextSlug === "", "openContext no mesmo slug nao alternou")

      castanha.openContext("outra")
      castanha.renamingSlug = "outra"
      castanha.deleteArmedSlug = "outra"
      castanha.closeContext()
      root.checar(castanha.contextSlug === "" && castanha.renamingSlug === ""
                  && castanha.deleteArmedSlug === "", "closeContext deixou estado preso")

      // Nome vazio é um não-ato: nada de comando com título em branco.
      root.lastCommand = ""
      castanha.renameMeeting("slug", "   ")
      castanha.renameCurrent("")
      root.checar(root.lastCommand === "", "nome vazio disparou comando")

      // A ativação genérica de Espaço/Enter no fundo não inicia captura.
      root.lastCommand = ""
      var panelWindow = null
      for (var j = 0; j < castanha.data.length; j++) {
        if (castanha.data[j].objectName === "castanhaKeyboardPanel") panelWindow = castanha.data[j]
      }
      root.checar(panelWindow !== null, "janela do painel não encontrada")
      if (panelWindow) {
        panelWindow.WlrLayershell.keyboardFocus = WlrKeyboardFocus.None
        panelWindow.mask = noInput
        panelWindow.focusTarget.activateRequested()
      }
      root.checar(root.lastCommand === "", "ativação genérica iniciou gravação")

      // O título da avulsa entra citado na linha de comando.
      castanha.adhocTitle = "Nome com 'aspas'"
      castanha.toggleRecording()
      root.checar(root.lastCommand.indexOf("castanha start --title=") === 0,
                  "titulo avulso: " + root.lastCommand)
      root.checar(root.lastCommand.indexOf("'\\''") >= 0,
                  "aspas nao escapadas: " + root.lastCommand)
      root.checar(castanha.adhocTitle === "", "rascunho do nome nao foi limpo")

      // Sem nome, o start é o de sempre.
      castanha.toggleRecording()
      root.checar(root.lastCommand === "castanha start", "start sem titulo: " + root.lastCommand)

      // ---- render: o painel aberto, com a nota que a CLI devolveu ----------
      castanha.open()
      if (panelWindow) {
        root.checar(panelWindow.screen && panelWindow.screen.width > 0,
                    "popup sem tela herdada da barra")
        root.checar(panelWindow.WlrLayershell.keyboardFocus === WlrKeyboardFocus.None,
                    "fixture tentou capturar teclado da sessão real")
      }
      root.esperarNota(0)
    }
  }

  // O `castanha notes --json` é um processo: a nota chega quando chega.
  function esperarNota(tentativa) {
    if (castanha.recentNotes.length > 0) return root.medirMenu()
    if (tentativa > 40) {
      root.falhas.push("a CLI nao devolveu nenhuma nota para o acervo de teste")
      return root.terminar()
    }
    esperaTimer.tentativa = tentativa
    esperaTimer.restart()
  }

  Timer {
    id: esperaTimer
    interval: 100
    property int tentativa: 0
    onTriggered: root.esperarNota(esperaTimer.tentativa + 1)
  }

  function medirMenu() {
    var slug = String(castanha.recentNotes[0].slug)
    root.medir(function(fechado) {
      root.checar(fechado > 0, "painel aberto nao renderizou nada: " + fechado)
      castanha.openContext(slug)
      root.medir(function(comMenu) {
        root.checar(comMenu > fechado, "menu de contexto nao renderizou: " + fechado + " -> " + comMenu)
        castanha.renamingSlug = slug
        root.medir(function(comCampo) {
          root.checar(comCampo > comMenu, "campo de renomear nao renderizou: " + comMenu + " -> " + comCampo)

          root.checar(castanha.editingNotes, "edicao nao protege a lista contra refresh")
          var originalNotes = castanha.recentNotes
          // A resposta da CLI em voo também deve preservar a edição aberta.
          root.refreshAndWait(function(depoisRefresh) {
            root.checar(castanha.recentNotes === originalNotes, "refresh recriou linhas durante edicao")
            root.checar(castanha.contextSlug === slug && castanha.renamingSlug === slug,
                        "refresh perdeu menu ou edicao")
            castanha.close()
            root.checar(castanha.contextSlug === "" && castanha.renamingSlug === "",
                        "fechar o painel nao desarmou o menu")
            root.refreshAndWait(function() {
              root.checar(castanha.recentNotes !== originalNotes && castanha.recentNotes.length > 0,
                          "controle positivo: refresh fora da edicao nao atualizou a lista")
              root.testDeleteAudio(slug, false)
            })
          })
        })
      })
    })
  }

  // CLI real no acervo isolado: apagar somente o arquivo escolhido e
  // apresentar erro quando o mesmo arquivo já não existe.
  function testDeleteAudio(slug, expectError) {
    var process = null
    for (var i = 0; i < castanha.data.length; i++) {
      if (castanha.data[i].objectName === "castanhaDeleteAudioProcess") process = castanha.data[i]
    }
    if (!process) { root.checar(false, "processo de exclusão ausente"); return root.terminar() }
    var completed = function(code) {
      process.exited.disconnect(completed)
      Qt.callLater(function() {
        if (expectError) {
          root.checar(code !== 0 && castanha.notesActionError !== "", "falha de exclusão não ficou visível")
          root.testRetry(slug, 0)
        } else {
          root.checar(code === 0 && castanha.notesActionError === "", "exclusão individual falhou")
          root.testDeleteAudio(slug, true)
        }
      })
    }
    process.exited.connect(completed)
    var state = root.prepareLibraryState(slug)
    castanha.deleteRecording(slug, "first.wav")
    root.checkLibraryInvalidated(state, "exclusão")
  }

  function prepareLibraryState(slug) {
    var library = null, player = null
    for (var i = 0; i < castanha.data.length; i++) {
      if (typeof castanha.data[i].prepareRecordingMutation === "function") library = castanha.data[i]
    }
    root.checar(!!library, "biblioteca compartilhada não encontrada")
    if (!library) return null
    for (var j = 0; j < library.data.length; j++) {
      if (library.data[j].objectName === "libraryMediaPlayer") player = library.data[j]
    }
    root.checar(!!player, "player compartilhado não encontrado")
    library.selectedSlug = slug
    library.detail = {slug: slug, title: "Fixture", status_label: "Fixture pronta", summary: "Resumo sintético anterior", transcript: "Texto.",
      decisions: [], action_items: [], warnings: [], segments: [], recordings: []}
    library.audioIndex = 0; library.playWhenReady = true; library.pendingSeek = 120
    if (player) { player.audioOutput.volume = 0; player.source = "file://" + Quickshell.env("CASTANHA_TEST_AUDIO") }
    return {library: library, player: player}
  }

  function checkLibraryInvalidated(state, operation) {
    if (!state) return
    root.checar(state.library.detail === null && state.library.audioIndex === -1
      && !state.library.playWhenReady && state.library.pendingSeek === -1,
      operation + " pelo painel não invalidou biblioteca compartilhada")
    root.checar(state.player && state.player.source.toString() === "", operation + " pelo painel deixou mídia carregada")
  }

  function testRetry(slug, index) {
    if (index >= 4) return root.terminar()
    var state = root.prepareLibraryState(slug)
    castanha.retryMeeting(slug)
    root.checkLibraryInvalidated(state, "retry")
    var process = null
    for (var i = 0; i < castanha.data.length; i++) {
      var obj = castanha.data[i]
      if (obj.command && obj.command.length > 1 && obj.command[1] === "retry") process = obj
    }
    if (!process) { root.checar(false, "processo retry não encontrado"); return root.terminar() }
    var completed = function(code) {
      process.exited.disconnect(completed)
      Qt.callLater(function() {
        if (index === 0) root.checar(code !== 0 && castanha.notesActionError === "Falha sintética de retry", "erro retry não ficou visível")
        else if (index === 3) root.checar(code !== 0 && castanha.notesActionError !== "" && castanha.notesActionError.indexOf("Sucesso") < 0, "rc1 retry aceitou mensagem de sucesso")
        else root.checar(code === 0 && castanha.notesActionError === "", "retry pending/empty virou falha")
        root.checar(castanha.retryingSlug === "", "retry deixou indicador preso")
        root.testRetry(slug, index + 1)
      })
    }
    process.exited.connect(completed)
  }

  function refreshAndWait(callback) {
    var loader = null
    for (var i = 0; i < castanha.data.length; i++) {
      if (castanha.data[i].objectName === "castanhaNotesProcess") loader = castanha.data[i]
    }
    if (!loader) {
      root.falhas.push("Process real da CLI nao encontrado")
      root.terminar()
      return
    }
    var completed = function(code, status) {
      loader.exited.disconnect(completed)
      root.checar(code === 0 && !loader.running, "CLI de refresh falhou ou nao terminou")
      root.medir(callback)
    }
    loader.exited.connect(completed)
    castanha.refreshNotes()
    root.checar(loader.running, "refresh nao iniciou a CLI real")
  }
}
