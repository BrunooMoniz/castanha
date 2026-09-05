# Castanha 🌰

Assistente executivo e gravador inteligente de reuniões para Linux e Omarchy.

Substituto aberto e nativo do Granola: grava chamadas sem bot, separa áudio em dois canais via PipeWire, sincroniza com o Google Calendar, notifica antes da reunião e transforma transcrições brutas em notas estruturadas (Bronze, Silver e Gold), prontas para o **Zinom** e para sua **LLM Wiki**.

<p align="center">
  <img src="preview.png" alt="Painel do Castanha no Omarchy" width="480">
</p>

---

## 🚀 Principais Recursos

1. **Captura Bot-Free via PipeWire**:
   - Grava silenciosamente chamadas no Google Meet, Teams, Zoom e WhatsApp Web/Desktop.
   - **Modo Duplo**: Canal esquerdo (microfone do usuário) e canal direito (áudio dos participantes remotos).
   - **Modo Presencial**: Grava apenas o microfone do computador para reuniões presenciais.
2. **Integração com Google Calendar**:
   - Sincronização direta via feed privado iCal ou integração com o hub Zinom.
   - Todos os eventos da agenda principal, inclusive os de dia inteiro e os sem link de chamada;
     o que não for reunião você esconde no painel (a série inteira, de uma vez).
   - Popup interativo 2 minutos antes com botão para entrar na chamada e botão para gravar.
   - Captura automática dos participantes (nomes e e-mails), pauta e links.
3. **Esteira Bronze -> Silver -> Gold**:
   - **Bronze**: Áudio original compactado em Opus + `metadata.json` + `transcript_raw.txt`.
   - **Silver**: Notas de reunião estruturadas em Markdown (YAML frontmatter, Resumo Executivo, Discussões, Decisões Tomadas e Ações).
   - **Gold**: Fatos atômicos preservados no Bronze/Gold, pendentes de suporte a origem no servidor. A entrega atual publica somente a nota narrativa via `remember`.
   - O áudio entra no Bronze antes de qualquer transcrição. Se ela falhar (sem internet, Groq fora
     do ar), o painel mostra "tentar de novo" e `castanha retry` reprocessa só o que faltou, sem
     apagar nada. Reunião longa vai em fatias para a Groq e as notas saem em partes (uma por
     minuto, no plano gratuito), então uma reunião de 2 h leva alguns minutos.
   - Sem chave da Groq e sem VPS, a transcrição falha declarada. O transcritor simulado só entra
     com `"provider": "mock"` na config (ou `CASTANHA_MOCK_TRANSCRIBER=1`), para teste.
4. **Plugin Nativo do Omarchy (Quickshell)**:
   - Widget discreto na barra com status ao vivo (`● REC 00:14:20`).
   - Painel popout com controle de gravação, próxima reunião e acesso rápido às notas.
   - Atalho global de teclado no Hyprland (`Super+Alt+R`).
5. **Backend Flexível**:
   - Roda 100% local ou envia o processamento pesado para uma VPS remota.

---

## 📦 Instalação

### Via Omarchy Plugin Marketplace (Recomendado)

```bash
omarchy plugin add https://github.com/BrunooMoniz/castanha --enable
~/.config/omarchy/plugins/io.github.brunoomoniz.castanha/setup
```

### Instalação Manual

```bash
git clone https://github.com/BrunooMoniz/castanha.git ~/.local/share/castanha
cd ~/.local/share/castanha && ./install.sh
```

Para adicionar o atalho global no Hyprland (`~/.config/hypr/hyprland.conf`):
```ini
bind = $mainMod ALT, R, exec, castanha toggle
```

---

## 🗑️ Remoção

Para desinstalar o plugin e o comando da máquina:
```bash
rm -f ~/.local/bin/castanha
omarchy plugin remove io.github.brunoomoniz.castanha
```

---

## 📋 Requisitos de Sistema

- **Omarchy** com omarchy-shell / Quickshell
- **PipeWire** com módulo pulse (`pactl`)
- **FFmpeg** e **ffprobe**
- **Python** 3.10 ou superior

---

## 🛠️ Uso via CLI

```bash
# Iniciar gravação de reunião
castanha start
castanha start --mic-only          # Modo presencial (somente microfone)
castanha start --title "Alinhamento com Time"

# Alternar gravação (inicia se ocioso, finaliza se gravando)
castanha toggle

# Pausar e retomar
castanha pause
castanha resume

# Finalizar e processar notas
castanha stop

# Retomar jobs e entregas pendentes, incluindo gravações sem notas
castanha sync --all
castanha sync --all --limit 20     # Limita pendências, não apenas reuniões recentes

# Consultar status
castanha status
castanha status --json

# Iniciar o daemon de calendário e notificações
castanha daemon --background

# Listar e abrir notas
castanha notes
castanha notes --open

# Segunda chance: transcrever de novo uma gravação que ficou sem notas
# (sem internet na hora do stop, Groq fora do ar, VPS lenta)
castanha retry                     # a última pendente (nada pendente = não faz nada)
castanha retry <slug>              # uma reunião específica; já transcrita, só refaz as notas
castanha retry --all               # todas as pendentes

# Gravar um evento da agenda com título, participantes e link dele
castanha start --event <uid>

# Reenviar ao Zinom uma reunião já transcrita
castanha sync [slug]
```

---

## ⚙️ Configuração

O arquivo de configuração vive em `~/.config/castanha/config.json`:

```json
{
  "storage": {
    "base_dir": "~/Notes/Meetings"
  },
  "calendar": {
    "feeds": [
      {
        "name": "Meu Calendário",
        "url": "https://calendar.google.com/calendar/ical/seu-email/private-xxx/basic.ics"
      }
    ]
  },
  "transcription": {
    "provider": "groq",
    "groq_api_key": "sua-chave-groq"
  },
  "llm": {
    "provider": "groq",
    "api_key": "sua-chave-groq",
    "model": "openai/gpt-oss-120b"
  },
  "zinom": {
    "enabled": true,
    "endpoint": "https://zinom.ai/mcp",
    "token": "seu-bearer-token"
  }
}
```

A captura preserva o áudio no Bronze antes da transcrição. Jobs remotos continuam
na VPS sem prender a conexão SSH; `castanha sync --all` consulta o resultado e
retoma checkpoints. SCP tem limite de 30 segundos e cada SSH, 15 segundos.
A VPS precisa de `flock`, `nohup` e do transcritor `/root/castanha-transcribe.py`.

Uma nota pode estar entregue enquanto seus fatos continuam em `pending_lineage`.
O Castanha não envia `brain_fact` até o servidor oferecer linhagem recuperável.
Transcrições mock ficam nos jobs locais e são excluídas das notas reais.
Notas longas são enviadas inteiras: eventual rejeição do servidor permanece erro
pendente, sem truncamento silencioso. Consulte `docs/F5-ENTREGA.md` para instalação
e reversão desta versão sem alterar a worktree XPS ativa.
