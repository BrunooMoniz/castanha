English | [Português (Brasil)](README.pt-BR.md)

# Castanha 🌰

**Record your meetings on Linux without inviting a bot.** Castanha sits in your
Omarchy bar, captures both sides of the call straight from PipeWire, and turns
the recording into structured notes you can read, search and keep.

No participant sees a "Castanha has joined the meeting" banner, because nothing
joins the meeting — the audio is captured on your own machine.

<p align="center">
  <img src="docs/images/panel.png" alt="The Castanha panel: next meeting, upcoming agenda and recent notes" width="380">
  &nbsp;&nbsp;
  <img src="docs/images/panel-recording.png" alt="Castanha recording: live timer, dual-channel capture and finish button" width="380">
</p>

<p align="center">
  <em>Idle, with the agenda and recent notes (left) and mid-recording, with the
  live timer (right). All data shown is simulated.</em>
</p>

---

## Start here: your first recording in 3 minutes

### 1. Install it

```bash
omarchy plugin add https://github.com/BrunooMoniz/castanha --enable
~/.config/omarchy/plugins/io.github.brunoomoniz.castanha/setup
```

The `setup` step is what makes the `castanha` command available and creates
your config file. It never overwrites an existing configuration.

### 2. Put the icon on the bar

```bash
omarchy bar put io.github.brunoomoniz.castanha
```

A microphone icon appears in your bar. That is the whole UI.

### 3. Record something

Click the icon and press **Start recording** — or bind a key:

```ini
# ~/.config/hypr/hyprland.conf
bind = $mainMod ALT, R, exec, castanha toggle
```

Talk for a minute, then press **Finish and save**. When it is done you have:

```
~/Notes/Meetings/
├── bronze/2026-02-17_0900_weekly-product-sync/   # audio + raw transcript
├── silver/2026-02-17_0900_weekly-product-sync.md # the readable note
└── gold/2026-02-17_0900_weekly-product-sync.json # extracted facts
```

Open the note with `castanha notes --open`. That's it — you are using Castanha.

> **Works out of the box?** Recording, the timer, the notes list and the audio
> diagnosis need nothing but FFmpeg and PipeWire. **Transcription and summaries
> need a provider** — see [Turning speech into notes](#turning-speech-into-notes)
> below. Until you configure one, Castanha keeps the audio safe in Bronze and
> tells you the transcription is pending instead of pretending it worked.

---

## What you actually get

**Both sides of the conversation, separated.** Castanha records your microphone
on one channel and the remote participants' audio on the other, so the
transcript can tell you apart from everyone else. Works with Google Meet,
Teams, Zoom and WhatsApp — anything that plays audio through PipeWire. For
in-person meetings, `castanha start --mic-only` records just the room.

**Notes, not a wall of text.** The raw transcript is only the first stage:

| Stage | What it is | Where it lands |
|---|---|---|
| **Bronze** | The evidence: Opus audio, `metadata.json`, raw transcript | `bronze/<slug>/` |
| **Silver** | The note a human reads: summary, decisions, action items | `silver/<slug>.md` |
| **Gold** | Atomic facts, each citing the verbatim passage it came from | `gold/<slug>.json` |

A Gold fact without a supporting verbatim passage is **dropped**, not guessed —
so nothing in the extracted facts is invented.

**Nothing is lost when something breaks.** The audio reaches Bronze before any
transcription is attempted. If your connection dies or the provider is down,
the panel shows *"Transcription pending — tries again"* and the audio waits.
`castanha retry` picks up exactly what was missing; it never deletes and never
starts over.

**It tells you when the audio was bad.** If your microphone was muted the whole
call, the note says so instead of leaving you with a silent file and no
explanation.

**Your calendar, if you want it.** Point Castanha at a private iCal feed (or
the Zinom hub) and the panel lists what's next, with a button to join the call
and a button to record it. Two minutes before a meeting you get a popup. Fully
optional — Castanha works fine as a manual recorder.

---

## Requirements

| | |
|---|---|
| **Omarchy** | with omarchy-shell / Quickshell |
| **PipeWire** | with the pulse module (`pactl`) |
| **FFmpeg** | plus `ffprobe` |
| **Python** | 3.10 or newer |

All four are already present on a standard Omarchy install. `setup` warns you
about anything missing instead of failing halfway through.

---

## Turning speech into notes

Castanha does not ship a transcription service. You choose one, and the choice
is explicit — it will never silently send your meetings somewhere you didn't
configure.

Edit `~/.config/castanha/config.json`:

```json
{
  "transcription": {
    "provider": "groq",
    "groq_api_key": "your-groq-key"
  },
  "llm": {
    "provider": "groq",
    "api_key": "your-groq-key",
    "model": "openai/gpt-oss-120b"
  }
}
```

That is the simplest working setup: [Groq](https://console.groq.com) transcribes
with Whisper and writes the summary. A free key is enough to start; long
meetings are sent in slices so a 2-hour call still completes.

**Prefer nothing leaving your machine?** Set `"provider": "vps_ssh"` and point
`vps_ssh_host` at a box you control — Castanha runs Whisper there over SSH,
resuming durable jobs instead of re-uploading. `deepgram` is also supported.

**Just testing?** `CASTANHA_MOCK_TRANSCRIBER=1` produces a fake transcript so
you can see the pipeline end to end. It is opt-in on purpose and never mixes
into real notes.

Your config file holds API keys, so Castanha creates it as an owner-only
`0600` file inside a `0700` directory, writes it atomically, and refuses to
read it through a symlink.

<details>
<summary><strong>Full configuration reference</strong></summary>

```json
{
  "storage": {
    "base_dir": "~/Notes/Meetings"
  },
  "audio": {
    "default_mode": "dual",
    "bitrate": "64k",
    "sample_rate": 48000,
    "format": "ogg"
  },
  "calendar": {
    "enabled": true,
    "notify_minutes_before": 2,
    "auto_record": false,
    "feeds": [
      {
        "name": "My Calendar",
        "url": "https://calendar.google.com/calendar/ical/.../basic.ics"
      }
    ]
  },
  "transcription": {
    "provider": "groq",
    "groq_api_key": "your-groq-key",
    "language": "auto"
  },
  "llm": {
    "provider": "groq",
    "api_key": "your-groq-key",
    "model": "openai/gpt-oss-120b"
  },
  "zinom": {
    "enabled": false,
    "endpoint": "https://zinom.ai/mcp",
    "token": ""
  }
}
```

- `storage.base_dir` — where Bronze/Silver/Gold live.
- `audio.default_mode` — `dual` (mic + call audio) or `mic_only` (in person).
- `transcription.language` — `auto` detects PT/EN/mixed; or pin `"pt"`, `"en"`.
- `calendar.auto_record` — start recording by itself when a meeting begins.
- `zinom` — optional sync to the [Zinom](https://zinom.ai) hub, off by default.

Remote transcription keeps running on the VPS without holding the SSH
connection open; `castanha sync --all` collects finished results and resumes
checkpoints. Who wrote each summary is recorded as `summary_provider` in the
meeting metadata.

</details>

---

## Everyday commands

You never need the terminal — the panel covers the common path — but the CLI is
the whole feature set.

```bash
castanha toggle              # start if idle, stop if recording (bind this)
castanha start --mic-only    # in-person meeting
castanha start --title "Team Sync"
castanha pause / resume
castanha stop                # stop and build the notes

castanha notes               # list meetings
castanha notes --open        # open the latest note
castanha notes <slug>        # everything about one meeting

castanha status              # what is happening right now (--json too)
castanha retry               # second chance for a pending transcription
castanha retry --all
castanha sync --all          # resume pending jobs and deliveries
```

<details>
<summary><strong>Calendar, renaming, cleanup and the daemon</strong></summary>

```bash
# Calendar
castanha agenda                    # upcoming meetings
castanha agenda refresh            # hit the calendars now
castanha agenda hide <uid>         # stop showing an event (whole series)
castanha agenda unhide <uid>       # show it again (--all restores everything)
castanha agenda hidden             # what you asked to hide
castanha start --event <uid>       # record with the event's title and attendees

# The notification daemon (needed for calendar popups and auto-record)
castanha daemon --background
castanha daemon --status
castanha daemon --stop

# Renaming: the folder name is identity and never changes, only the title
castanha rename current "Better Name"   # while recording
castanha rename last "Better Name"
castanha rename <slug> "Better Name"

# Cleanup — deletions go to base_dir/.trash/, never rm -rf
castanha delete-recording <slug>   # free space, keep the notes
castanha delete-meeting <slug>     # remove the meeting (recoverable)
castanha recordings list <slug>
castanha recordings delete <slug> [file]

# Append another recording to an existing meeting
castanha start --meeting <slug>
```

</details>

---

## Uninstalling

```bash
rm -f ~/.local/bin/castanha
omarchy plugin remove io.github.brunoomoniz.castanha
```

Your meetings in `~/Notes/Meetings` and your config in `~/.config/castanha` are
**left alone** — removing the plugin never deletes your recordings. Delete
those two directories yourself if you want them gone.

---

## Privacy and consent

Castanha records audio on your machine. **Recording a conversation without
telling the other participants is illegal in many places** — one-party versus
two-party consent varies by country and by state. Castanha does not announce
itself in the call, so telling people is your job, and you should do it.

What leaves your computer, and only if you configure it: the audio goes to
whichever transcription provider you set, and the transcript goes to whichever
LLM provider you set. With `vps_ssh` that is a machine you own. With no
provider configured, nothing is sent anywhere. Zinom sync is off by default.

The screenshots in this README were generated from simulated data by
`scripts/gerar-capturas.py`, which builds a throwaway `HOME` with invented
meetings — no real meeting has ever appeared in them.

---

## Troubleshooting

**The icon isn't on the bar.** `omarchy bar put io.github.brunoomoniz.castanha`,
then `omarchy-shell shell rescanPlugins`. QML changes need a full
`omarchy-restart-shell`.

**`castanha: command not found`.** The `setup` step was skipped, or
`~/.local/bin` is not on your `PATH`.

**The recording is silent.** Check `castanha notes <slug>` — Castanha diagnoses
the audio and will tell you if the microphone was muted at the system or
keyboard level.

**Notes never appear.** `castanha status` shows the current state and
`castanha retry` retries a pending transcription. Your audio is already safe in
`bronze/<slug>/`.

---

## Development

```bash
git clone https://github.com/BrunooMoniz/castanha.git
cd castanha && ./install.sh          # symlinks the plugin into Omarchy

python3 -m unittest discover tests   # ⚠️ starts a REAL recording
python3 -m unittest tests.test_storage tests.test_cli tests.test_plugin_layout
python3 scripts/gerar-capturas.py    # regenerate the README screenshots
```

The full suite exercises the live capture path and will leave a real meeting in
`~/Notes/Meetings`; prefer the specific modules while iterating. Panel tests
need a Wayland compositor.

Architecture and design notes live in [`docs/`](docs/). Bug reports and pull
requests are welcome.

---

## License

[MIT](LICENSE) — Bruno Moniz.

**External dependencies:** FFmpeg/ffprobe (LGPL/GPL), PipeWire via `pactl`
(MIT), Python 3 standard library (PSF), Quickshell/Qt at runtime (LGPL).
Optional third-party services, used only when you configure them: Groq,
Deepgram, Zinom. Castanha bundles none of them and ships no credentials.
