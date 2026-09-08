.pragma library

var STRINGS = {
  "mode.mic_only": { "en": "mic only", "pt": "somente microfone" },
  "mode.dual": { "en": "mic + call", "pt": "microfone + chamada" },

  "status.recording": { "en": "recording", "pt": "gravando" },
  "status.paused": { "en": "paused", "pt": "pausado" },
  "status.saving": { "en": "saving", "pt": "salvando" },
  "status.idle": { "en": "idle", "pt": "ocioso" },

  "hero.processing": { "en": "transcribing and writing the notes", "pt": "transcrevendo e escrevendo as notas" },
  "hero.mic_muted": { "en": "microphone muted in system", "pt": "microfone mudo no sistema" },
  "hero.next_meeting": { "en": "next meeting in {n} min", "pt": "próxima reunião em {n} min" },
  "hero.ready": { "en": "ready to record", "pt": "pronto para gravar" },

  "bar.tooltip_recording": { "en": "Recording for {t} · click for panel, right-click to finish", "pt": "Gravando há {t} · clique para o painel, direito para finalizar" },
  "bar.tooltip_paused": { "en": "Recording paused at {t}", "pt": "Gravação pausada em {t}" },
  "bar.tooltip_processing": { "en": "Processing meeting notes", "pt": "Processando as notas da reunião" },
  "bar.tooltip_mic_muted": { "en": "Castanha · microphone is muted", "pt": "Castanha · o microfone está mudo" },
  "bar.tooltip_next_meeting": { "en": "{title} in {n} min", "pt": "{title} em {n} min" },
  "bar.tooltip_idle": { "en": "Castanha · click for panel, right-click to record", "pt": "Castanha · clique para o painel, direito para gravar" },

  "mic_warning.body": { "en": "Microphone is muted. The recording will be silent until you unmute.", "pt": "Microfone mudo. A gravação sai em silêncio até você desmutar." },

  "btn.finish_save": { "en": "Finish and save", "pt": "Finalizar e salvar" },
  "btn.start_recording": { "en": "Start recording", "pt": "Iniciar gravação" },
  "btn.resume": { "en": "Resume recording", "pt": "Retomar a gravação" },
  "btn.pause": { "en": "Pause recording", "pt": "Pausar a gravação" },
  "btn.join_call": { "en": "Join call", "pt": "Entrar na chamada" },
  "btn.open_notes_folder": { "en": "Open notes folder", "pt": "Abrir a pasta de notas" },
  "btn.record_meeting": { "en": "Record meeting", "pt": "Gravar reunião" },
  "btn.on_google": { "en": "On Google", "pt": "No Google" },
  "btn.hide_from_castanha": { "en": "Hide from Castanha", "pt": "Ocultar do Castanha" },
  "btn.play_recording": { "en": "Play recording", "pt": "Ouvir gravação" },
  "btn.delete_recording": { "en": "Delete this audio (keeps notes and transcript)", "pt": "Apagar este áudio (mantém notas e transcrição)" },
  "btn.reprocessing": { "en": "Reprocessing…", "pt": "Reprocessando…" },
  "btn.retry": { "en": "Retry upload/transcription", "pt": "Tentar upload/transcrição de novo" },
  "btn.notes": { "en": "Notes", "pt": "Notas" },
  "btn.transcript": { "en": "Transcript", "pt": "Transcrição" },
  "btn.folder": { "en": "Folder", "pt": "Pasta" },

  "panel.current_meeting": { "en": "CURRENT MEETING", "pt": "REUNIÃO ATUAL" },
  "panel.adhoc_recording": { "en": "Ad-hoc recording", "pt": "Gravação avulsa" },
  "panel.upcoming_meetings": { "en": "UPCOMING MEETINGS", "pt": "PRÓXIMAS REUNIÕES" },
  "panel.recent_notes": { "en": "RECENT NOTES", "pt": "NOTAS RECENTES" },
  "panel.participants_one": { "en": "1 PARTICIPANT", "pt": "1 PARTICIPANTE" },
  "panel.participants_many": { "en": "{n} PARTICIPANTS", "pt": "{n} PARTICIPANTES" },
  "panel.summary": { "en": "SUMMARY", "pt": "RESUMO" },

  "agenda.refreshing": { "en": "Refreshing calendars…", "pt": "Atualizando agendas…" },
  "agenda.refresh_now": { "en": "Refresh calendars now", "pt": "Atualizar agendas agora" },
  "agenda.unavailable": { "en": "Calendar unavailable: {err}", "pt": "Agenda indisponível: {err}" },
  "agenda.empty": { "en": "Nothing in the next few hours", "pt": "Nada nas próximas horas" },

  "meeting.all_day_short": { "en": "Day", "pt": "Dia" },
  "meeting.all_day": { "en": "All day", "pt": "Dia inteiro" },
  "meeting.time_range": { "en": "{start} to {end}", "pt": "{start} às {end}" },
  "meeting.hide_series": { "en": "Don't show this event again (the whole series)", "pt": "Não mostrar mais este evento (a série inteira)" },
  "meeting.no_guests": { "en": "No guests in this event.", "pt": "Sem convidados neste evento." },
  "meeting.default_title": { "en": "Meeting", "pt": "Reunião" },

  "tooltip.view_meeting_details": { "en": "View meeting details", "pt": "Ver os detalhes da reunião" },
  "tooltip.reprocessing": { "en": "Reprocessing upload/transcription…", "pt": "Reprocessando upload/transcrição…" },
  "tooltip.retry_upload": { "en": "Retry upload and transcription", "pt": "Tentar upload e transcrição novamente" },

  "count.participants_one": { "en": "{n} participant", "pt": "{n} participante" },
  "count.participants_many": { "en": "{n} participants", "pt": "{n} participantes" },

  "rsvp.accepted": { "en": "accepted", "pt": "aceitou" },
  "rsvp.declined": { "en": "declined", "pt": "recusou" },
  "rsvp.tentative": { "en": "maybe", "pt": "talvez" },
  "rsvp.needs_action": { "en": "no reply", "pt": "sem resposta" },

  "attendee.organizer_suffix": { "en": "  (organizer)", "pt": "  (organizador)" },

  "zinom.sending": { "en": "Sending to Zinom…", "pt": "Enviando ao Zinom…" },
  "zinom.resend": { "en": "Send this meeting to Zinom again", "pt": "Enviar esta reunião ao Zinom de novo" },

  "note.no_audio": { "en": "no audio", "pt": "sem áudio" },
  "note.recordings_plural": { "en": "{n} recordings", "pt": "{n} gravações" },
  "note.reprocessing": { "en": "Reprocessing upload and transcription…", "pt": "Reprocessando upload e transcrição…" },
  "note.upload_pending": { "en": "Upload/transcription pending: 󰑐 tries again", "pt": "Upload/transcrição pendente: 󰑐 tenta de novo" },
  "note.transcription_pending": { "en": "Remote transcription is still processing: 󰑐 follows the same job", "pt": "Transcrição remota ainda em processamento: 󰑐 acompanha o mesmo job" },
  "note.collapse_details": { "en": "Collapse meeting details", "pt": "Recolher detalhes da reunião" },
  "note.view_details": { "en": "View meeting details", "pt": "Ver detalhes da reunião" },

  "meta.mode_mic": { "en": "mic mode", "pt": "modo microfone" },
  "meta.mode_call": { "en": "call mode", "pt": "modo chamada" },

  "recordings.header_one": { "en": "1 AUDIO RECORDING", "pt": "1 GRAVAÇÃO DE ÁUDIO" },
  "recordings.header_zero": { "en": "AUDIO", "pt": "ÁUDIO" },
  "recordings.header_many": { "en": "{n} AUDIO RECORDINGS", "pt": "{n} GRAVAÇÕES DE ÁUDIO" },
  "recordings.deleted_notice": { "en": "Audio deleted to free space (notes and transcript preserved).", "pt": "Áudio removido para liberar espaço (notas e transcrição preservadas)." },

  "when.today": { "en": "today {t}", "pt": "hoje {t}" },
  "when.yesterday": { "en": "yesterday {t}", "pt": "ontem {t}" },

  "audio_status.ok": { "en": "Audio captured on both channels.", "pt": "Áudio capturado nos dois canais." },
  "audio_status.mic_mudo": { "en": "The microphone channel was silent: the mic was muted (keyboard or system). Only the call audio was recorded.", "pt": "O canal do microfone saiu em silêncio: o mic estava mudo (teclado ou sistema). Só o áudio da chamada foi gravado." },
  "audio_status.sem_audio": { "en": "No channel captured audio: the recording is silent from start to end.", "pt": "Nenhum canal captou áudio: a gravação está em silêncio do início ao fim." },
  "audio_status.desconhecido": { "en": "Could not measure audio levels.", "pt": "Não foi possível medir os níveis do áudio." },
  "audio_status.audio_apagado": { "en": "Audio deleted to free space (notes and transcript preserved).", "pt": "Gravação de áudio apagada (notas e transcrição preservadas)" },

  "delivery.summary_pending": { "en": "Summary pending", "pt": "Resumo pendente" },
  "delivery.tombstoned": { "en": "Deleted in Zinom", "pt": "Excluído no Zinom" },
  "delivery.superseded": { "en": "Superseded in Zinom", "pt": "Substituído no Zinom" },
  "delivery.pending": { "en": "Zinom delivery pending", "pt": "Envio ao Zinom pendente" },
  "delivery.not_sent": { "en": "Not sent to Zinom: {reason}", "pt": "Não enviado ao Zinom: {reason}" },
  "delivery.no_reason": { "en": "no stated reason", "pt": "sem motivo declarado" },
  "delivery.failed": { "en": "Not saved in Zinom ({reason})", "pt": "Não salvou no Zinom ({reason})" },
  "delivery.unconfirmed": { "en": "Zinom delivery unconfirmed", "pt": "Envio ao Zinom não confirmado" },
  "delivery.saved_facts_pending": { "en": "Note saved in Zinom; facts pending", "pt": "Nota salva no Zinom; fatos pendentes" },
  "delivery.saved_one_fact": { "en": "Saved in Zinom, with {n} fact", "pt": "Salvo no Zinom, com {n} fato" },
  "delivery.saved_many_facts": { "en": "Saved in Zinom, with {n} facts", "pt": "Salvo no Zinom, com {n} fatos" },
  "delivery.saved": { "en": "Saved in Zinom", "pt": "Salvo no Zinom" }
};

function t(key, lang) {
    var entry = STRINGS[key];
    if (!entry) return key;
    var s = entry[lang] || entry.en;
    return s !== undefined ? s : key;
}
