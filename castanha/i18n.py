"""Catálogo de mensagens do Castanha: en padrão, pt-BR quando o locale é pt."""

import os
import re

STRINGS = {
    # Audio (persistido em metadata.json como audio_diagnostico)
    "audio_status.ok": {
        "en": "Audio captured on both channels.",
        "pt": "Áudio capturado nos dois canais.",
    },
    "audio_status.mic_mudo": {
        "en": "The microphone channel was silent: the mic was muted (keyboard or system). Only the call audio was recorded.",
        "pt": "O canal do microfone saiu em silêncio: o mic estava mudo (teclado ou sistema). Só o áudio da chamada foi gravado.",
    },
    "audio_status.sem_audio": {
        "en": "No channel captured audio: the recording is silent from start to end.",
        "pt": "Nenhum canal captou áudio: a gravação está em silêncio do início ao fim.",
    },
    "audio_status.desconhecido": {
        "en": "Could not measure audio levels.",
        "pt": "Não foi possível medir os níveis do áudio.",
    },
    "audio_status.audio_apagado": {
        "en": "Audio deleted to free space (notes and transcript preserved).",
        "pt": "Gravação de áudio apagada (notas e transcrição preservadas)",
    },
    # Notificações (engine)
    "notify.unavailable": {
        "en": "[Castanha] Notification unavailable ({exc}); processing continues",
        "pt": "[Castanha] Notificação indisponível ({exc}); processamento continua",
    },
    "notify.mic_muted_title": {
        "en": "Muted Microphone! 🔇",
        "pt": "Microfone mudo! 🔇",
    },
    "notify.mic_muted_body": {
        "en": "The microphone is muted in the system or on the keyboard. Unmute before speaking, otherwise the recording will be silent.",
        "pt": "O microfone está mudo no sistema ou no teclado. Desmute antes de falar, senão a gravação sai em silêncio.",
    },
    "notify.started_title": {
        "en": "Recording Started 🌰",
        "pt": "Gravação Iniciada 🌰",
    },
    "notify.started_body": {
        "en": "{title}\nMode: {mode_label}",
        "pt": "{title}\nModo: {mode_label}",
    },
    "notify.paused_title": {
        "en": "Recording Paused ⏸️",
        "pt": "Gravação Pausada ⏸️",
    },
    "notify.paused_body": {
        "en": "Click Resume when you continue.",
        "pt": "Clique em Retomar quando continuar.",
    },
    "notify.resumed_title": {
        "en": "Recording Resumed ▶️",
        "pt": "Gravação Retomada ▶️",
    },
    "notify.resumed_body": {
        "en": "Capturing meeting audio.",
        "pt": "Capturando áudio da reunião.",
    },
    "notify.finishing_title": {
        "en": "Finalizing Meeting ⏳",
        "pt": "Finalizando Reunião ⏳",
    },
    "notify.finishing_body": {
        "en": "Processing transcription and generating notes...",
        "pt": "Processando transcrição e gerando notas...",
    },
    "notify.preserved_title": {
        "en": "Recording preserved, processing pending",
        "pt": "Gravação preservada, processamento pendente",
    },
    "notify.notes_ready_title": {
        "en": "Notes Ready! 🌰",
        "pt": "Notas Prontas! 🌰",
    },
    "notify.reprocessing_title": {
        "en": "Reprocessing Meeting ⏳",
        "pt": "Reprocessando Reunião ⏳",
    },
    "notify.reprocessing_body": {
        "en": "{title}\nSending for transcription and generating notes...",
        "pt": "{title}\nEnviando para transcrição e gerando notas...",
    },
    "notify.reprocess_fail_title": {
        "en": "Failed to reprocess ⚠️",
        "pt": "Falha ao reprocessar ⚠️",
    },
    "engine.problem_summary_pending": {
        "en": "Summary pending: {exc}",
        "pt": "Resumo pendente: {exc}",
    },
    "engine.problem_transcription_failed": {
        "en": "transcription failed ({errors})",
        "pt": "a transcrição falhou ({errors})",
    },
    "engine.problem_transcription_pending": {
        "en": "transcription pending ({errors})",
        "pt": "a transcrição está pendente ({errors})",
    },
    "engine.problem_mock_transcription": {
        "en": "Simulated transcript kept separately, not sent to memory",
        "pt": "Transcrição simulada preservada separadamente, não enviada à memória",
    },
    "sync.reason_finalizer_active": {
        "en": "Finalizer active or identity unavailable",
        "pt": "Finalizador ativo ou identidade indisponível",
    },
    "retry.reason_checkpoint_unavailable": {
        "en": "Checkpoint unavailable; files preserved",
        "pt": "Checkpoint indisponível; arquivos preservados",
    },
    "notify.reprocess_fail_body": {
        "en": "{title}\nThe audio could not be transcribed.",
        "pt": "{title}\nNão foi possível transcrever o áudio.",
    },
    "notify.reprocessed_title": {
        "en": "Meeting Reprocessed! 🌰",
        "pt": "Reunião Reprocessada! 🌰",
    },
    "notify.reprocessed_body": {
        "en": "{title}\nNotes and facts updated.",
        "pt": "{title}\nNotas e fatos atualizados.",
    },
    "mode.dual": {
        "en": "Microphone + Call",
        "pt": "Microfone + Chamada",
    },
    "mode.mic_only": {
        "en": "Microphone Only",
        "pt": "Somente Microfone",
    },
    # Daemon
    "daemon.already_running": {
        "en": "[Castanha] A daemon is already running (PID {pid}). This one does not start.",
        "pt": "[Castanha] Já existe um daemon rodando (PID {pid}). Este não sobe.",
    },
    "daemon.calendar_error": {
        "en": "[Castanha Daemon] Error checking calendar: {error}",
        "pt": "[Castanha Daemon] Erro ao checar calendário: {error}",
    },
    "daemon.no_attendees": {
        "en": "No attendees",
        "pt": "Sem convidados",
    },
    "daemon.action_record": {
        "en": "Record Meeting 🌰",
        "pt": "Gravar Reunião 🌰",
    },
    "daemon.action_join": {
        "en": "Join Call 🌐",
        "pt": "Entrar na Chamada 🌐",
    },
    "daemon.meeting_soon_title": {
        "en": "Meeting Starting Soon: {title}",
        "pt": "Reunião em Instantes: {title}",
    },
    "daemon.meeting_soon_body": {
        "en": "Attendees: {attendees}\nClick to join or start recording.",
        "pt": "Participantes: {attendees}\nClique para entrar ou iniciar a gravação.",
    },
    # CLI: start
    "cli.start.event_not_in_agenda": {
        "en": "[Castanha] Event {event} is not in the loaded agenda; recording by title.",
        "pt": "[Castanha] Evento {event} não está na agenda carregada; gravando pelo título.",
    },
    "cli.start.success": {
        "en": "🌰 Recording started successfully! [PID: {pid}]",
        "pt": "🌰 Gravação iniciada com sucesso! [PID: {pid}]",
    },
    "cli.start.title": {
        "en": "📌 Title: {title}",
        "pt": "📌 Título: {title}",
    },
    "cli.start.mode": {
        "en": "🎙️ Mode: {mode}",
        "pt": "🎙️ Modo: {mode}",
    },
    "cli.start.linked": {
        "en": "🔗 Linked to meeting: {slug}",
        "pt": "🔗 Vinculada à reunião: {slug}",
    },
    "cli.start.mic_muted": {
        "en": "🔇 WARNING: the microphone is MUTED (system/keyboard). Unmute before speaking.",
        "pt": "🔇 ATENÇÃO: o microfone está MUDO no sistema/teclado. Desmute antes de falar.",
    },
    "cli.erro": {
        "en": "❌ Error: {message}",
        "pt": "❌ Erro: {message}",
    },
    # CLI: stop
    "cli.stop.processing": {
        "en": "⏳ Finalizing the meeting and processing notes...",
        "pt": "⏳ Finalizando reunião e processando notas...",
    },
    "cli.stop.success": {
        "en": "✅ Meeting finished successfully!",
        "pt": "✅ Reunião finalizada com sucesso!",
    },
    "cli.stop.with_problems": {
        "en": "⚠️  Meeting finished WITH PROBLEMS:",
        "pt": "⚠️  Reunião finalizada COM PROBLEMA:",
    },
    "cli.stop.bronze": {
        "en": "📁 Bronze: {dir}",
        "pt": "📁 Bronze: {dir}",
    },
    "cli.stop.silver": {
        "en": "📝 Silver (Notes): {file}",
        "pt": "📝 Silver (Notas): {file}",
    },
    "cli.stop.gold": {
        "en": "🧠 Gold (Facts): {file}",
        "pt": "🧠 Gold (Fatos): {file}",
    },
    # CLI: pause / resume
    "cli.pause.done": {
        "en": "⏸️ Recording paused.",
        "pt": "⏸️ Gravação pausada.",
    },
    "cli.resume.done": {
        "en": "▶️ Recording resumed.",
        "pt": "▶️ Gravação retomada.",
    },
    # CLI: status
    "cli.status.label": {
        "en": "Status: {status}",
        "pt": "Status: {status}",
    },
    "cli.status.idle": {
        "en": "⚪ Idle",
        "pt": "⚪ Ocioso",
    },
    "cli.status.recording": {
        "en": "🔴 Recording",
        "pt": "🔴 Gravando",
    },
    "cli.status.paused": {
        "en": "⏸️ Paused",
        "pt": "⏸️ Pausado",
    },
    "cli.status.processing": {
        "en": "⏳ Processing",
        "pt": "⏳ Processando",
    },
    "cli.status.elapsed": {
        "en": "Elapsed time: {timer}",
        "pt": "Tempo decorrido: {timer}",
    },
    "cli.status.mode": {
        "en": "Mode: {mode}",
        "pt": "Modo: {mode}",
    },
    "cli.status.meeting": {
        "en": "Meeting: {title}",
        "pt": "Reunião: {title}",
    },
    "cli.status.next_meeting": {
        "en": "Next meeting: {title} ({start})",
        "pt": "Próxima reunião: {title} ({start})",
    },
    # CLI: daemon
    "cli.daemon.not_running": {
        "en": "No daemon running.",
        "pt": "Nenhum daemon rodando.",
    },
    "cli.daemon.stopped": {
        "en": "🌰 Daemon stopped (PID {pid}).",
        "pt": "🌰 Daemon encerrado (PID {pid}).",
    },
    "cli.daemon.running": {
        "en": "🌰 Daemon running (PID {pid}).",
        "pt": "🌰 Daemon rodando (PID {pid}).",
    },
    "cli.daemon.already_running": {
        "en": "🌰 A daemon is already running (PID {pid}). Nothing to do.",
        "pt": "🌰 Já existe um daemon rodando (PID {pid}). Nada a fazer.",
    },
    "cli.daemon.background_started": {
        "en": "🌰 Castanha daemon started in the background.",
        "pt": "🌰 Daemon do Castanha iniciado em segundo plano.",
    },
    "cli.daemon.foreground": {
        "en": "🌰 Starting the Castanha daemon in the foreground (Ctrl+C to stop)...",
        "pt": "🌰 Iniciando daemon do Castanha em primeiro plano (Ctrl+C para encerrar)...",
    },
    # CLI: notes
    "cli.notes.not_found": {
        "en": "Meeting '{slug}' not found.",
        "pt": "Reunião '{slug}' não encontrada.",
    },
    "cli.notes.meta": {
        "en": "   Date: {when} · Duration: {duration}s · Mode: {mode}",
        "pt": "   Data: {when} · Duração: {duration}s · Modo: {mode}",
    },
    "cli.notes.audio_status": {
        "en": "   Audio status: {status}",
        "pt": "   Status do áudio: {status}",
    },
    "cli.notes.attendees": {
        "en": "   Attendees:",
        "pt": "   Participantes:",
    },
    "cli.notes.no_name": {
        "en": "No name",
        "pt": "Sem nome",
    },
    "cli.notes.recordings": {
        "en": "   Audio recordings:",
        "pt": "   Gravações de áudio:",
    },
    "cli.notes.recordings_none": {
        "en": "   Audio recordings: (none on disk / deleted)",
        "pt": "   Gravações de áudio: (nenhuma no disco / apagada)",
    },
    "cli.notes.summary": {
        "en": "   Executive Summary:\n     {preview}",
        "pt": "   Resumo Executivo:\n     {preview}",
    },
    "cli.notes.silver_path": {
        "en": "   Silver notes: {path}",
        "pt": "   Notas Silver: {path}",
    },
    "cli.notes.transcript": {
        "en": "   Transcript: {path}",
        "pt": "   Transcrição: {path}",
    },
    "cli.notes.gold_path": {
        "en": "   Gold facts: {path}",
        "pt": "   Fatos Gold: {path}",
    },
    "cli.notes.none_yet": {
        "en": "No meeting notes recorded yet.",
        "pt": "Nenhuma nota de reunião gravada ainda.",
    },
    "cli.notes.recent": {
        "en": "Recent recorded meetings:",
        "pt": "Últimas reuniões registradas:",
    },
    "cli.notes.recordings_count": {
        "en": " ({count} recording(s))",
        "pt": " ({count} gravação/ões)",
    },
    # Storage: renomear / apagar reunião
    "storage.rename.bad_slug": {
        "en": "Invalid meeting identifier: '{slug}'.",
        "pt": "Identificador de reunião inválido: '{slug}'.",
    },
    "storage.rename.empty_title": {
        "en": "The new name cannot be empty.",
        "pt": "O novo nome não pode ser vazio.",
    },
    "storage.meeting_not_found": {
        "en": "Meeting '{slug}' not found.",
        "pt": "Reunião '{slug}' não encontrada.",
    },
    # CLI: delete-recording
    "cli.none_found": {
        "en": "No meeting found.",
        "pt": "Nenhuma reunião encontrada.",
    },
    "cli.delete.success": {
        "en": "🗑️ Recording '{file}' deleted successfully from meeting '{slug}'.",
        "pt": "🗑️ Gravação '{file}' apagada com sucesso da reunião '{slug}'.",
    },
    "cli.delete.remaining": {
        "en": "   {count} recording(s) still remain in this meeting.",
        "pt": "   Ainda restam {count} gravação(ões) nesta reunião.",
    },
    "cli.delete.preserved": {
        "en": "   Silver notes, transcript and Zinom data were preserved.",
        "pt": "   As notas em Silver, transcrição e dados no Zinom foram preservados.",
    },
    # CLI: rename / delete-meeting
    "cli.rename.ok": {
        "en": "✏️ '{previous}' is now '{title}' ({slug}).",
        "pt": "✏️ '{previous}' agora é '{title}' ({slug}).",
    },
    "cli.rename.current": {
        "en": "✏️ Current recording renamed to '{title}'.",
        "pt": "✏️ Gravação em curso renomeada para '{title}'.",
    },
    "cli.rename.no_recording": {
        "en": "No recording in progress to rename.",
        "pt": "Nenhuma gravação em andamento para renomear.",
    },
    "cli.delete_meeting.ok": {
        "en": "🗑️ Meeting '{slug}' moved to the trash: {dir}",
        "pt": "🗑️ Reunião '{slug}' movida para a lixeira: {dir}",
    },
    "cli.help.rename": {
        "en": "Rename a meeting (slug, 'last' or 'current' for the live recording)",
        "pt": "Renomeia uma reunião (slug, 'last' ou 'current' para a gravação em curso)",
    },
    "cli.help.rename_title": {
        "en": "New meeting name",
        "pt": "Novo nome da reunião",
    },
    "cli.help.delete_meeting": {
        "en": "Move a whole meeting (audio, notes and transcript) to the trash",
        "pt": "Move a reunião inteira (áudio, notas e transcrição) para a lixeira",
    },
    # CLI: recordings
    "cli.recordings.none": {
        "en": "No audio recordings found for '{slug}'.",
        "pt": "Nenhuma gravação de áudio encontrada para '{slug}'.",
    },
    "cli.recordings.list": {
        "en": "Recordings of meeting '{slug}':",
        "pt": "Gravações da reunião '{slug}':",
    },
    # CLI: sync
    "cli.sync.no_pending": {
        "en": "No pending deliveries in this run.",
        "pt": "Nenhuma entrega pendente nesta execução.",
    },
    "cli.sync.no_meetings": {
        "en": "No meetings recorded yet.",
        "pt": "Nenhuma reunião gravada ainda.",
    },
    "cli.sync.ok": {
        "en": "✅ {slug}: saved to Zinom",
        "pt": "✅ {slug}: salvo no Zinom",
    },
    "cli.sync.facts": {
        "en": ", with {count} fact(s)",
        "pt": ", com {count} fato(s)",
    },
    "cli.sync.pending": {
        "en": "⏳ {slug}: pending ({reason})",
        "pt": "⏳ {slug}: pendente ({reason})",
    },
    "cli.sync.pending_default_reason": {
        "en": "awaiting retry",
        "pt": "aguardando nova tentativa",
    },
    "cli.sync.tombstoned": {
        "en": "⏭️  {slug}: deletion in Zinom preserved",
        "pt": "⏭️  {slug}: exclusão no Zinom preservada",
    },
    "cli.sync.skipped": {
        "en": "⏭️  {slug}: not sent ({reason})",
        "pt": "⏭️  {slug}: não enviado ({reason})",
    },
    "cli.sync.error": {
        "en": "❌ {slug}: failed to save",
        "pt": "❌ {slug}: não salvou",
    },
    # CLI: retry
    "cli.retry.nothing_pending": {
        "en": "Nothing pending: no meeting with audio awaiting transcription.",
        "pt": "Nada pendente: nenhuma reunião com áudio aguardando transcrição.",
    },
    "cli.retry.reprocessing": {
        "en": "⏳ Reprocessing meeting '{slug}'...",
        "pt": "⏳ Reprocessando reunião '{slug}'...",
    },
    "cli.retry.success": {
        "en": "✅ {slug}: audio transcribed and notes generated successfully!",
        "pt": "✅ {slug}: áudio transcrito e notas geradas com sucesso!",
    },
    "cli.retry.partial": {
        "en": "⚠️  {slug}: reprocessed with observations:",
        "pt": "⚠️  {slug}: reprocessado com observações:",
    },
    "cli.retry.bronze": {
        "en": "   📁 Bronze: {dir}",
        "pt": "   📁 Bronze: {dir}",
    },
    "cli.retry.silver": {
        "en": "   📝 Silver: {file}",
        "pt": "   📝 Silver: {file}",
    },
    "cli.retry.gold": {
        "en": "   🧠 Gold:   {file}",
        "pt": "   🧠 Gold:   {file}",
    },
    "cli.retry.error": {
        "en": "❌ {slug}: error reprocessing ({message})",
        "pt": "❌ {slug}: erro ao reprocessar ({message})",
    },
    # CLI: agenda
    "cli.agenda.none": {
        "en": "No meetings in the next {hours}h.",
        "pt": "Nenhuma reunião nas próximas {hours}h.",
    },
    "cli.agenda.upcoming": {
        "en": "Upcoming meetings ({hours}h):",
        "pt": "Próximas reuniões ({hours}h):",
    },
    "cli.agenda.attendees": {
        "en": " · {count} attendees",
        "pt": " · {count} participantes",
    },
    "cli.agenda.refreshed": {
        "en": "Agenda updated: {count} meeting(s) found in the next {hours}h.",
        "pt": "Agenda atualizada: {count} reunião(ões) encontrada(s) nas próximas {hours}h.",
    },
    "cli.agenda.refresh_error": {
        "en": "Error updating agenda: {error}",
        "pt": "Erro ao atualizar agenda: {error}",
    },
    # CLI: agenda hide/unhide/hidden
    "cli.hidden.need_key": {
        "en": "❌ A uid or series_key is required.",
        "pt": "❌ Precisa de um uid ou series_key.",
    },
    "cli.hidden.hidden": {
        "en": "🙈 Hidden: {title}",
        "pt": "🙈 Escondido: {title}",
    },
    "cli.hidden.hint": {
        "en": "   Applies to the whole series. To restore: castanha agenda unhide {chave}",
        "pt": "   Vale para a série inteira. Para voltar: castanha agenda unhide {chave}",
    },
    "cli.unhide.all": {
        "en": "👁️  {count} event(s) visible again.",
        "pt": "👁️  {count} evento(s) voltaram a aparecer.",
    },
    "cli.unhide.back": {
        "en": "👁️  Back on the agenda.",
        "pt": "👁️  De volta à agenda.",
    },
    "cli.unhide.not_hidden": {
        "en": "That event was not hidden.",
        "pt": "Esse evento não estava escondido.",
    },
    "cli.unhide.need_key": {
        "en": "❌ Provide the key or use --all.",
        "pt": "❌ Informe a chave ou use --all.",
    },
    "cli.hidden_list.none": {
        "en": "Nothing hidden.",
        "pt": "Nada escondido.",
    },
    "cli.hidden_list.title": {
        "en": "Events you asked not to show:",
        "pt": "Eventos que você mandou não mostrar:",
    },
    "cli.hidden_list.no_title": {
        "en": "(no title)",
        "pt": "(sem título)",
    },
    # CLI: argparse
    "cli.help.description": {
        "en": "Castanha: meeting recorder and assistant for Omarchy/Linux",
        "pt": "Castanha: Gravador e assistente de reuniões para Omarchy/Linux",
    },
    "cli.help.commands": {
        "en": "Available commands",
        "pt": "Comandos disponíveis",
    },
    "cli.help.start": {
        "en": "Start recording a meeting",
        "pt": "Inicia a gravação de uma reunião",
    },
    "cli.help.mode": {
        "en": "Recording mode",
        "pt": "Modo de gravação",
    },
    "cli.help.mic_only": {
        "en": "Shortcut for microphone-only mode (in person)",
        "pt": "Atalho para modo apenas microfone (presencial)",
    },
    "cli.help.title": {
        "en": "Meeting title",
        "pt": "Título da reunião",
    },
    "cli.help.meeting": {
        "en": "Existing meeting slug to append the recording to",
        "pt": "Slug de reunião existente para adicionar a gravação",
    },
    "cli.help.event": {
        "en": "uid of an agenda event: the recording takes its title, attendees and link",
        "pt": "uid de um evento da agenda: a gravação leva título, participantes e link dele",
    },
    "cli.help.stop": {
        "en": "Stop the recording and process the notes",
        "pt": "Finaliza a gravação e processa as notas",
    },
    "cli.help.pause": {
        "en": "Pause the recording",
        "pt": "Pausa a gravação",
    },
    "cli.help.resume": {
        "en": "Resume the recording",
        "pt": "Retoma a gravação",
    },
    "cli.help.toggle": {
        "en": "Start or stop the recording (ideal for a keyboard shortcut)",
        "pt": "Inicia ou para a gravação (ideal para atalho de teclado)",
    },
    "cli.help.status": {
        "en": "Show the recorder's current status",
        "pt": "Exibe o status atual do gravador",
    },
    "cli.help.json": {
        "en": "Output in JSON format",
        "pt": "Saída em formato JSON",
    },
    "cli.help.daemon": {
        "en": "Manage the background daemon",
        "pt": "Gerencia o daemon em background",
    },
    "cli.help.daemon_background": {
        "en": "Run detached in the background",
        "pt": "Roda em segundo plano desanexado",
    },
    "cli.help.daemon_stop": {
        "en": "Stop the running daemon",
        "pt": "Encerra o daemon que estiver rodando",
    },
    "cli.help.daemon_status": {
        "en": "Report whether the daemon is running, and its PID",
        "pt": "Diz se há daemon rodando, e qual PID",
    },
    "cli.help.config": {
        "en": "Manage settings",
        "pt": "Gerencia configurações",
    },
    "cli.help.notes": {
        "en": "List recent meetings or show details of a meeting",
        "pt": "Lista as últimas reuniões ou exibe detalhes de uma reunião",
    },
    "cli.help.notes_slug": {
        "en": "Meeting slug for full details",
        "pt": "Slug da reunião para ver detalhes completos",
    },
    "cli.help.notes_open": {
        "en": "Open the notes folder in the file manager",
        "pt": "Abre a pasta de notas no gerenciador de arquivos",
    },
    "cli.help.notes_json": {
        "en": "JSON output (used by the Omarchy plugin)",
        "pt": "Saída em JSON (usada pelo plugin do Omarchy)",
    },
    "cli.help.notes_limit": {
        "en": "How many meetings to list",
        "pt": "Quantas reuniões listar",
    },
    "cli.help.delete_recording": {
        "en": "Delete a meeting's audio recording without deleting the notes",
        "pt": "Apaga uma gravação de áudio de uma reunião sem apagar as notas",
    },
    "cli.help.slug_last": {
        "en": "Meeting slug (or 'last' for the latest)",
        "pt": "Slug da reunião (ou 'last' para a última)",
    },
    "cli.help.arquivo": {
        "en": "Specific audio filename (e.g. audio.ogg)",
        "pt": "Nome do arquivo de áudio específico (ex: audio.ogg)",
    },
    "cli.help.recordings": {
        "en": "Manage meeting audio recordings",
        "pt": "Gerencia gravações de áudio das reuniões",
    },
    "cli.help.recordings_list": {
        "en": "List a meeting's audio recordings",
        "pt": "Lista gravações de áudio de uma reunião",
    },
    "cli.help.slug": {
        "en": "Meeting slug",
        "pt": "Slug da reunião",
    },
    "cli.help.recordings_delete": {
        "en": "Delete a meeting's audio recording",
        "pt": "Apaga uma gravação de áudio de uma reunião",
    },
    "cli.help.sync": {
        "en": "Resend a meeting to Zinom (the latest, by default)",
        "pt": "Reenvia uma reunião ao Zinom (a última, por padrão)",
    },
    "cli.help.sync_slug": {
        "en": "Meeting slug; empty = the latest",
        "pt": "Slug da reunião; vazio = a última",
    },
    "cli.help.sync_all": {
        "en": "Resend everything still pending",
        "pt": "Reenvia tudo que ficou pendente",
    },
    "cli.help.sync_limit": {
        "en": "Max pending items per run; default: all",
        "pt": "Máximo de pendências por execução; padrão: todas",
    },
    "cli.help.json_short": {
        "en": "JSON output",
        "pt": "Saída em JSON",
    },
    "cli.help.retry": {
        "en": "Try resending audio for transcription and processing notes/facts/Zinom",
        "pt": "Tenta reenviar áudio para transcrição e processar notas/fatos/Zinom",
    },
    "cli.help.retry_slug": {
        "en": "Meeting slug (or 'last' for the latest); without a slug, the latest pending one",
        "pt": "Slug da reunião (ou 'last' para a última reunião); sem slug, a última pendente",
    },
    "cli.help.retry_all": {
        "en": "Reprocess every meeting pending transcription",
        "pt": "Reprocessa todas as reuniões pendentes de transcrição",
    },
    "cli.help.retry_limit": {
        "en": "How many meetings to scan with --all",
        "pt": "Quantas reuniões olhar com --all",
    },
    "cli.help.agenda": {
        "en": "Upcoming meetings from Google accounts connected to Zinom",
        "pt": "Próximas reuniões das contas Google conectadas no Zinom",
    },
    "cli.help.horas": {
        "en": "Window in hours",
        "pt": "Janela em horas",
    },
    "cli.help.agenda_refresh": {
        "en": "Fetch the calendars now and update the state",
        "pt": "Bate nas agendas agora e atualiza o estado",
    },
    "cli.help.agenda_hide": {
        "en": "Stop showing an event (the whole series)",
        "pt": "Para de mostrar um evento (a série inteira)",
    },
    "cli.help.hide_uid": {
        "en": "event uid or series_key (the panel sends the series_key)",
        "pt": "uid ou series_key do evento (o painel manda o series_key)",
    },
    "cli.help.hide_title": {
        "en": "Title, just so you recognize it in the list",
        "pt": "Título, só para você reconhecer na lista",
    },
    "cli.help.agenda_unhide": {
        "en": "Show a hidden event again",
        "pt": "Volta a mostrar um evento escondido",
    },
    "cli.help.unhide_uid": {
        "en": "Key to show again; empty with --all clears everything",
        "pt": "Chave a reexibir; vazio com --all limpa tudo",
    },
    "cli.help.unhide_all": {
        "en": "Show everything again",
        "pt": "Volta a mostrar todos",
    },
    "cli.help.agenda_hidden": {
        "en": "List what you asked not to show",
        "pt": "Lista o que você mandou não mostrar",
    },
    "cli.help.refresh_agenda": {
        "en": "Refresh the agenda immediately from accounts and feeds",
        "pt": "Atualiza a agenda imediatamente nas contas e feeds",
    },
}

_LOCALE_PREFIX = re.compile(r"^[A-Za-z]{2}(_|-|$)")


def resolve_locale() -> str:
    override = os.environ.get("CASTANHA_LANG")
    if override:
        return _match_locale(override)
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if value:
            return _match_locale(value)
    return "en"


def _match_locale(value: str) -> str:
    if _LOCALE_PREFIX.match(value) and value[:2].lower() == "pt":
        return "pt"
    return "en"


def t(key: str, locale: str = None, **fmt) -> str:
    locale = locale or resolve_locale()
    entry = STRINGS.get(key)
    if entry is None:
        return key
    text = entry.get(locale) or entry["en"]
    if fmt:
        return text.format(**fmt)
    return text


def audio_status_message(status, locale: str = None) -> str:
    key = f"audio_status.{status}"
    if key not in STRINGS:
        return ""
    return t(key, locale=locale)
