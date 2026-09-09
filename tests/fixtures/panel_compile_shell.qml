import QtQuick
import Quickshell
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

  // A barra é injetada pelo shell em produção. Aqui só o que o Panel lê.
  QtObject {
    id: fakeBar
    readonly property color foreground: Color.foreground
    readonly property color barForeground: Color.foreground
    readonly property color urgent: Color.urgent
    readonly property string fontFamily: Style.font.family
    function run(cmd) { root.lastCommand = cmd }
    function switchPanelFrom(panel, direction) {}
  }

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

  FloatingWindow {
    visible: true
    implicitWidth: 400
    implicitHeight: 700

    CastanhaPanel {
      id: castanha
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

          // Fechar o painel desarma o menu: reabrir não volta armado.
          castanha.close()
          root.checar(castanha.contextSlug === "" && castanha.renamingSlug === "",
                      "fechar o painel nao desarmou o menu")
          root.terminar()
        })
      })
    })
  }
}
