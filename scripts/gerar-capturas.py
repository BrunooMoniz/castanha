"""Gera as capturas de tela do README a partir de um acervo 100% fictício.

Não toca no acervo real: monta um HOME temporário (com XDG_CONFIG_HOME e
XDG_STATE_HOME dentro dele) contendo reuniões inventadas — nomes genéricos,
e-mails `@example.com`, nenhuma organização real —, sobe o Panel.qml de verdade
ancorado numa barra layer-shell e exporta **apenas o card do painel** pelo renderizador Qt.

Por que HOME falso: o painel deriva caminhos de HOME, então apontar só os XDG_*
deixava a captura sair com a agenda real de quem rodou o script.

A exportação grabToImage usa somente o componente Qt. Mesmo que o usuário
feche o painel durante a captura, nenhuma outra janela entra na imagem.

Uso:
    python3 scripts/gerar-capturas.py [--saida docs/images]

Requer: quickshell e um compositor Wayland (WAYLAND_DISPLAY).
"""

import argparse
import json
import math
import threading
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OMARCHY_SHELL = Path("/usr/share/omarchy/shell")

# O script roda de qualquer cwd; o pacote está na raiz do repositório.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Pessoas e reuniões inventadas de propósito. `example.com` é reservado para
# documentação (RFC 2606), então nenhum endereço aqui alcança alguém real.
ADA = {"name": "Ada Lima", "email": "ada@example.com"}
RAVI = {"name": "Ravi Nakamura", "email": "ravi@example.com"}
SOFIA = {"name": "Sofia Braun", "email": "sofia@example.com"}


def _participante(p, status="accepted", organizer=False):
    return {**p, "status": status, "organizer": organizer, "self": False}


def reunioes(agora: datetime) -> list:
    """Notas prontas no acervo, datadas em relação a `agora`."""
    return [
        {
            "slug": "2026-02-17_0900_weekly-product-sync",
            "title": "Alinhamento de produto",
            "recorded_at": (agora - timedelta(hours=2)).isoformat(timespec="seconds"),
            "duration_seconds": 1_920,
            "attendees": [_participante(ADA, organizer=True), _participante(RAVI),
                          _participante(SOFIA, status="tentative")],
            "summary": ("A equipe decidiu liberar o novo cadastro gradualmente "
                        "e adiar a migração de cobrança até concluir a página "
                        "de preços.\n"),
            "decisions": ["Liberar o novo cadastro gradualmente.",
                          "Adiar a migração de cobrança para o próximo ciclo."],
            "actions": ["Ada: preparar a versão e habilitar o cadastro internamente.",
                        "Ravi: documentar a reversão da migração de cobrança."],
        },
        {
            "slug": "2026-02-16_1430_design-review-checkout",
            "title": "Revisão do checkout",
            "recorded_at": (agora - timedelta(days=1, hours=3)).isoformat(timespec="seconds"),
            "duration_seconds": 2_640,
            "attendees": [_participante(SOFIA, organizer=True), _participante(ADA)],
            "summary": "Clientes recorrentes terão um checkout mais curto.\n",
            "decisions": ["Pular o endereço para clientes recorrentes."],
            "actions": ["Sofia: atualizar o protótipo com o fluxo em duas etapas."],
        },
        {
            "slug": "2026-02-16_1100_infra-oncall-handoff",
            "title": "Passagem de plantão",
            "recorded_at": (agora - timedelta(days=1, hours=6)).isoformat(timespec="seconds"),
            "duration_seconds": 780,
            "attendees": [_participante(RAVI, organizer=True)],
            "summary": "Fila normalizada; alerta repetitivo silenciado por 24 horas.\n",
            "decisions": ["Silence the disk-pressure alert on staging for 24h."],
            "actions": ["Ravi: file a ticket to fix the alert threshold."],
            # Uma pendência de propósito: a captura mostra a "segunda chance"
            # que o painel oferece de verdade quando a transcrição não saiu.
            "pending": True,
        },
    ]


def agenda(agora: datetime) -> list:
    """Próximos eventos, em relação a `agora`, no formato do MeetingEvent."""
    from castanha.calendar import MeetingEvent

    eventos = [
        MeetingEvent(
            uid="demo-roadmap-1",
            title="Planejamento trimestral",
            start=agora + timedelta(minutes=18),
            end=agora + timedelta(minutes=78),
            attendees=[_participante(ADA, organizer=True),
                       _participante(RAVI, status="needsAction")],
            organizer=ADA["email"],
            conference_url="https://meet.example.com/quarterly-roadmap",
            description="Plan the next quarter.",
            location="https://meet.example.com/quarterly-roadmap",
            calendar_name="Trabalho",
            account="you@example.com",
            conference_provider="meet",
        ),
        MeetingEvent(
            uid="demo-mentoring-2",
            title="Mentoria individual",
            start=agora + timedelta(hours=3),
            end=agora + timedelta(hours=3, minutes=30),
            attendees=[_participante(SOFIA, organizer=True)],
            organizer=SOFIA["email"],
            calendar_name="Trabalho",
            account="you@example.com",
        ),
    ]
    return [e.to_dict() for e in eventos]


def montar_acervo(base: Path, gravando: bool = False) -> dict:
    """HOME fictício com Bronze/Silver/Gold que a CLI real sabe listar."""
    agora = datetime.now().replace(microsecond=0)
    home = base / "home"
    xdg_config, xdg_state = home / ".config", home / ".local" / "state"
    meetings = home / "Notes" / "Meetings"
    bronze, silver, gold = meetings / "bronze", meetings / "silver", meetings / "gold"
    for d in (bronze, silver, gold, xdg_state / "castanha"):
        d.mkdir(parents=True, exist_ok=True)

    cfg_dir = xdg_config / "castanha"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(json.dumps({
        "storage": {"base_dir": str(meetings), "bronze_dir": str(bronze),
                    "silver_dir": str(silver), "gold_dir": str(gold)},
        # Nada de rede na captura: a agenda vem do state, já pronta.
        "calendar": {"enabled": False, "zinom": {"enabled": False}},
        "transcription": {"provider": "mock"},
        "zinom": {"enabled": False},
    }, indent=2), encoding="utf-8")
    os.chmod(cfg_dir / "config.json", 0o600)

    for r in reunioes(agora):
        slug = r["slug"]
        dir_bronze = bronze / slug
        dir_bronze.mkdir(parents=True, exist_ok=True)
        (dir_bronze / "audio.ogg").write_bytes(b"OggS" + b"\0" * 2048)
        pendente = bool(r.get("pending"))
        (dir_bronze / "transcript_raw.txt").write_text(
            "" if pendente else f"[00:00] {ADA['name']}: {r['summary'].strip()}\n",
            encoding="utf-8")

        (dir_bronze / "metadata.json").write_text(json.dumps({
            "title": r["title"], "slug": slug, "recorded_at": r["recorded_at"],
            "duration_seconds": r["duration_seconds"], "mode": "dual",
            "audio_status": "ok", "attendees": r["attendees"],
            "transcription_provider": "pending" if pendente else "whisper-large-v3",
            "transcription_pending": pendente,
            "transcription_pending_reason": ("Sem conexão; processamento preservado para retomar"
                                             if pendente else None),
            "summary_provider": None if pendente else "claude-opus-5",
            "processing_status": "pending" if pendente else "complete",
            "recordings": [{
                "id": "audio.ogg", "filename": "audio.ogg",
                "path": str(dir_bronze / "audio.ogg"),
                "size_bytes": 2052, "size_human": "2.0 KB",
                "duration_seconds": r["duration_seconds"],
                "transcription_provider": "pending" if pendente else "whisper-large-v3",
            }],
            "zinom": {} if pendente else {"status": "ok", "document_id": "demo-doc-0001"},
        }, indent=2, ensure_ascii=False), encoding="utf-8")

        if pendente:
            continue

        pessoas = ", ".join(a["name"] for a in r["attendees"])
        (silver / f"{slug}.md").write_text(
            f'---\ntitle: "{r["title"]}"\ndate: {r["recorded_at"]}\n'
            f'attendees: [{pessoas}]\n---\n\n'
            f'# {r["title"]}\n\n## Resumo Executivo\n\n{r["summary"]}\n'
            "## Decisões\n\n" + "".join(f"- {d}\n" for d in r["decisions"]) +
            "\n## Próximos passos\n\n" + "".join(f"- {a}\n" for a in r["actions"]),
            encoding="utf-8")
        (gold / f"{slug}.json").write_text(json.dumps({
            "title": r["title"], "slug": slug,
            "facts": [{"fact": d, "verbatim": d} for d in r["decisions"]],
        }, indent=2), encoding="utf-8")

    proximos = agenda(agora)
    estado = {
        "status": "idle",
        "upcoming_meetings": proximos,
        "next_meeting": proximos[0],
        "agenda_updated_at": agora.isoformat(timespec="seconds"),
    }
    if gravando:
        # Gravação em curso: o painel troca o herói pelo cronômetro e pelo
        # medidor de áudio, que é o que o usuário vê 90% do tempo.
        comeco = agora - timedelta(minutes=12, seconds=34)
        estado.update({
            "status": "recording",
            "started_at": comeco.isoformat(timespec="seconds"),
            "current_meeting": {
                "title": "Planejamento trimestral",
                "attendees": [_participante(ADA, organizer=True), _participante(RAVI)],
                "conference_url": "https://meet.example.com/quarterly-roadmap",
                "uid": "demo-roadmap-1",
            },
            "mode": "dual",
            "mic_device_name": "Microfone interno · Dell XPS",
            "call_device_name": "Áudio do sistema · saída selecionada",
            "audio_peak": {"mic": 0.42, "system": 0.61,
                           "at": agora.isoformat(timespec="seconds")},
        })

    (xdg_state / "castanha" / "state.json").write_text(
        json.dumps(estado, indent=2, ensure_ascii=False), encoding="utf-8")

    return {"home": home, "xdg_config": xdg_config, "xdg_state": xdg_state,
            "meetings": meetings}


def montar_qml(destino: Path) -> Path:
    destino.mkdir(parents=True, exist_ok=True)
    (destino / "Ui").symlink_to(OMARCHY_SHELL / "Ui", target_is_directory=True)
    (destino / "Commons").symlink_to(OMARCHY_SHELL / "Commons", target_is_directory=True)
    for source in [*ROOT.glob("*.qml"), *ROOT.glob("*.js")]:
        if source.name != "Panel.qml":
            (destino / source.name).symlink_to(source)
    # O tipo QML vem do nome do arquivo; "Panel" colidiria com o Ui/Panel.qml
    # do shell, que é a base do nosso.
    source = (ROOT / "Panel.qml").read_text()
    source = source.replace("  function cardGeometry() {", """  function fixtureCapture(path) {
    if (!root.opened) { root.open(); return false }
    return keyCatcher.parent.parent.grabToImage(function(result) {
      if (root.opened && result.saveToFile(path)) console.log("CASTANHA_PRONTO")
    })
  }
  function cardGeometry() {""")
    (destino / "CastanhaPanel.qml").write_text(source)
    (destino / "shell.qml").write_text(CENA, encoding="utf-8")
    return destino / "shell.qml"


# A cena sobe o painel como o shell sobe: uma barra layer-shell real ancorando
# o popout. Num FloatingWindow o KeyboardPanel não mapeia — ele é um
# PanelWindow e tira o `screen` do anchorWindow, que numa janela flutuante vem
# nulo, deixando o card 0x0 e a captura em branco.
CENA = """
import QtQuick
import Quickshell
import Quickshell.Wayland
import qs.Commons
import qs.Ui

ShellRoot {
  id: root

  PanelWindow {
    id: barra
    screen: Quickshell.screens[0]
    anchors { top: true; left: true; right: true }
    implicitHeight: 30
    color: "transparent"
    WlrLayershell.namespace: "castanha-preview-bar"
    WlrLayershell.layer: WlrLayer.Top

    // O painel lê `bar` para tema, fonte e posição, e chama run() nos botões.
    property color foreground: Color.foreground
    property color barForeground: Color.foreground
    property color urgent: Color.urgent
    property string fontFamily: Style.font.family
    property string position: "top"
    function run(cmd) { console.log("CMD " + cmd) }
    function switchPanelFrom(panel, direction) {}

    CastanhaPanel {
      id: castanha
      anchors.right: parent.right
      anchors.rightMargin: 24
      anchors.verticalCenter: parent.verticalCenter
      bar: barra
      manageIpc: false
    }
  }

  Timer {
    interval: 500
    running: true
    onTriggered: { castanha.open(); espera.restart() }
  }

  Timer {
    id: espera
    interval: 200
    property int tentativa: 0
    onTriggered: {
      if (castanha.recentNotes.length > 0 || tentativa > 40) assentar.restart()
      else { tentativa = tentativa + 1; espera.restart() }
    }
  }

  // O card informa onde está: o recorte usa a geometria dele, nunca a tela.
  Timer {
    id: assentar
    interval: 900
    onTriggered: {
      var card = castanha.cardGeometry()
      if (!card) { console.log("CASTANHA_SEM_GEOMETRIA"); return }
      console.log("CASTANHA_GEOMETRIA " + Math.round(card.x) + "," + Math.round(card.y)
                  + " " + Math.round(card.width) + "x" + Math.round(card.height))
      castanha.fixtureCapture(Quickshell.env("CASTANHA_PREVIEW_OUTPUT"))
    }
  }
}
"""

CENAS = [
    ("panel.png", False),
    ("panel-recording.png", True),
]


def capturar(saida: Path) -> list:
    if not shutil.which("quickshell"):
        sys.exit("quickshell não encontrado")
    if not os.environ.get("WAYLAND_DISPLAY"):
        sys.exit("sem WAYLAND_DISPLAY: o painel exige um compositor Wayland")

    saida.mkdir(parents=True, exist_ok=True)
    feitas = []
    for nome, gravando in CENAS:
        feitas.append(_uma_cena(saida / nome, gravando))
    return feitas


def _uma_cena(destino: Path, gravando: bool) -> Path:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        acervo = montar_acervo(base, gravando=gravando)
        shell = montar_qml(base / "qml")

        env = os.environ.copy()
        env["QT_QPA_PLATFORM"] = "wayland"
        # HOME falso junto dos XDG_*: o painel deriva caminhos de HOME também,
        # e sem isto a captura sairia com a agenda real de quem rodou.
        env["HOME"] = str(acervo["home"])
        env["XDG_CONFIG_HOME"] = str(acervo["xdg_config"])
        env["XDG_STATE_HOME"] = str(acervo["xdg_state"])
        env["PATH"] = f"{ROOT / 'bin'}:{env.get('PATH', '')}"
        env["CASTANHA_LANG"] = "pt"
        env["CASTANHA_PREVIEW_OUTPUT"] = str(destino.resolve())

        stop_fixture = threading.Event()
        def fixture_levels():
            state_path = acervo["xdg_state"] / "castanha" / "state.json"
            initial = json.loads(state_path.read_text())
            tick = 0
            while not stop_fixture.is_set():
                tick += 1
                initial.update(mic_peak=abs(math.sin(tick * .61)) * .55,
                               call_peak=abs(math.sin(tick * .29 + 1)) * .35,
                               mic_peak_updated_at=time.time(), call_peak_updated_at=time.time())
                temporary = state_path.with_suffix(".preview-tmp")
                temporary.write_text(json.dumps(initial))
                temporary.replace(state_path)
                stop_fixture.wait(.1)
        worker = threading.Thread(target=fixture_levels, daemon=True) if gravando else None
        if worker:
            worker.start()

        proc = subprocess.Popen(
            ["quickshell", "--no-duplicate", "--path", str(shell), "--no-color"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            geometria = ""
            fim = time.time() + 45
            while time.time() < fim:
                linha = proc.stdout.readline()
                if not linha:
                    break
                print(linha.rstrip(), file=sys.stderr)
                if "CASTANHA_GEOMETRIA" in linha:
                    geometria = linha.split("CASTANHA_GEOMETRIA", 1)[1].strip()
                if "CASTANHA_PRONTO" in linha:
                    break
            if not geometria:
                proc.kill()
                sys.exit(f"o painel não informou a geometria do card ({destino.name})")

            if not destino.exists():
                sys.exit("a captura do componente não foi concluída")
        finally:
            stop_fixture.set()
            if worker:
                worker.join(timeout=2)
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    return destino


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--saida", default=str(ROOT / "docs" / "images"))
    args = ap.parse_args()
    for p in capturar(Path(args.saida)):
        print(f"capturado: {p}")
