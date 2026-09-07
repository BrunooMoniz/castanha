English | [Português (Brasil)](README.pt-BR.md)

# Castanha 🌰

Executive assistant and smart meeting recorder for Linux and Omarchy.

An open, native replacement for Granola: it records calls without a bot, splits audio into two channels via PipeWire, syncs with Google Calendar, notifies you before the meeting, and turns raw transcripts into structured notes (Bronze, Silver and Gold), ready for **Zinom** and your **LLM Wiki**. English by default, Brazilian Portuguese when your locale is `pt_BR`.

<p align="center">
  <img src="preview.png" alt="Castanha panel on Omarchy" width="480">
</p>

---

## 🚀 Key Features

1. **Bot-Free Capture via PipeWire**:
   - Silently records calls on Google Meet, Teams, Zoom and WhatsApp Web/Desktop.
   - **Dual Mode**: left channel (the user's microphone) and right channel (the remote participants' audio).
   - **In-Person Mode**: records only the computer's microphone for in-person meetings.
2. **Google Calendar Integration**:
   - Direct sync via a private iCal feed or integration with the Zinom hub.
   - Every event from your calendars (the primary one of each account and the ones you can edit, such as
     a company group calendar), including all-day events and events without a call link;
     whatever is not a meeting you hide in the panel (the whole series, in one go).
   - Interactive popup 2 minutes before, with a button to join the call and a button to record.
   - Automatic capture of attendees (names and emails), agenda and links.
3. **Bronze -> Silver -> Gold Pipeline**:
   - **Bronze**: original audio compressed to Opus + `metadata.json` + `transcript_raw.txt`.
   - **Silver**: structured meeting notes in Markdown (YAML frontmatter, Executive Summary, Discussions, Decisions Made and Actions).
   - **Gold**: atomic facts preserved in Bronze/Gold, pending origin support on the server. The current delivery publishes only the narrative note via `remember`.
   - Audio lands in Bronze before any transcription. If transcription fails (no internet,
     Groq down), the panel shows "try again" and `castanha retry` reprocesses only what was
     missing, without deleting anything. A long meeting goes to Groq in slices and the notes
     come out in parts (one per minute, on the free plan), so a 2-hour meeting takes a few
     minutes.
   - Without a Groq key and without a VPS, the transcription failure is declared. The mock
     transcriber only comes in with `"provider": "mock"` in the config (or
     `CASTANHA_MOCK_TRANSCRIBER=1`), for testing.
4. **Native Omarchy Plugin (Quickshell)**:
   - Discreet bar widget with live status (`● REC 00:14:20`).
   - Popout panel with recording control, the next meeting and quick access to the notes.
   - Global keyboard shortcut in Hyprland (`Super+Alt+R`).
5. **Flexible Backend**:
   - Runs 100% locally or sends heavy processing to a remote VPS.

---

## 📦 Installation

### Via the Omarchy Plugin Marketplace (Recommended)

```bash
omarchy plugin add https://github.com/BrunooMoniz/castanha --enable
~/.config/omarchy/plugins/io.github.brunoomoniz.castanha/setup
```

### Manual Installation

```bash
git clone https://github.com/BrunooMoniz/castanha.git ~/.local/share/castanha
cd ~/.local/share/castanha && ./install.sh
```

To add the global shortcut in Hyprland (`~/.config/hypr/hyprland.conf`):
```ini
bind = $mainMod ALT, R, exec, castanha toggle
```

---

## 🗑️ Removal

To uninstall the plugin and the command from the machine:
```bash
rm -f ~/.local/bin/castanha
omarchy plugin remove io.github.brunoomoniz.castanha
```

---

## 📋 System Requirements

- **Omarchy** with omarchy-shell / Quickshell
- **PipeWire** with the pulse module (`pactl`)
- **FFmpeg** and **ffprobe**
- **Python** 3.10 or higher

---

## 🛠️ CLI Usage

```bash
# Start recording a meeting
castanha start
castanha start --mic-only          # In-person mode (microphone only)
castanha start --title "Team Sync"

# Toggle recording (starts when idle, stops when recording)
castanha toggle

# Pause and resume
castanha pause
castanha resume

# Stop and process the notes
castanha stop

# Resume pending jobs and deliveries, including recordings without notes
castanha sync --all
castanha sync --all --limit 20     # Limits the backlog, not just recent meetings

# Check status
castanha status
castanha status --json

# Start the calendar and notification daemon
castanha daemon --background
castanha daemon --stop             # Stop the running daemon
castanha daemon --status           # Whether a daemon is running, and which PID

# List and open notes
castanha notes
castanha notes --open
castanha notes <slug>              # Full details of one meeting

# Second chance: transcribe again a recording that was left without notes
# (no internet at stop time, Groq down, slow VPS)
castanha retry                     # the latest pending one (nothing pending = does nothing)
castanha retry <slug>              # a specific meeting; if already transcribed, only rebuilds the notes
castanha retry --all               # all pending ones

# Record a calendar event with its title, attendees and link
castanha start --event <uid>

# Append the recording to an existing meeting
castanha start --meeting <slug>

# Send an already-transcribed meeting to Zinom again
castanha sync [slug]

# Upcoming meetings from the Google accounts connected in Zinom
castanha agenda
castanha agenda refresh            # Hit the calendars now and update the state
castanha agenda hide <uid>         # Stop showing an event (the whole series)
castanha agenda unhide <uid>       # Show a hidden event again (--all shows everything again)
castanha agenda hidden             # List what you asked not to show
castanha refresh-agenda            # Refresh the agenda now across accounts and feeds

# Delete a meeting's audio recording without deleting the notes
castanha delete-recording <slug>

# Manage a meeting's audio recordings
castanha recordings list <slug>
castanha recordings delete <slug> [file]
```

---

## ⚙️ Configuration

The configuration file lives at `~/.config/castanha/config.json`:

```json
{
  "storage": {
    "base_dir": "~/Notes/Meetings"
  },
  "calendar": {
    "feeds": [
      {
        "name": "My Calendar",
        "url": "https://calendar.google.com/calendar/ical/your-email/private-xxx/basic.ics"
      }
    ]
  },
  "transcription": {
    "provider": "groq",
    "groq_api_key": "your-groq-key"
  },
  "llm": {
    "provider": "groq",
    "api_key": "your-groq-key",
    "model": "openai/gpt-oss-120b"
  },
  "zinom": {
    "enabled": true,
    "endpoint": "https://zinom.ai/mcp",
    "token": "your-bearer-token"
  }
}
```

Capture preserves the audio in Bronze before transcription. Remote jobs keep
running on the VPS without holding the SSH connection; `castanha sync --all`
checks the result and resumes checkpoints. SCP has a 30-second limit and each
SSH command, 15 seconds. The VPS needs `flock`, `nohup` and the transcriber
`/root/castanha-transcribe.py`.

A note can be delivered while its facts remain in `pending_lineage`. Castanha
does not send `brain_fact` until the server offers recoverable lineage. Mock
transcripts stay in local jobs and are excluded from real notes. Long notes
are sent whole: any server rejection remains a pending error, with no silent
truncation. See `docs/F5-ENTREGA.md` for installing and rolling back this
version without changing the active XPS worktree.
