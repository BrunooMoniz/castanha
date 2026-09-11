.pragma library

function stages(note) {
    var n = note || {}, z = n.zinom || {}
    if (["invalidated", "empty", "rebuilding"].indexOf(n.content_status) >= 0) return [
        {label: "Áudio", state: n.recordings_count > 0 ? "done" : "removed"},
        {label: "Transcrição", state: n.content_status === "empty" ? "removed" : "waiting"},
        {label: "Resumo", state: n.content_status === "empty" ? "removed" : "waiting"},
        {label: "Zinom", state: n.cleanup_status === "pending" || z.status === "pending_cleanup" ? "waiting" : "removed"}
    ]
    var transcript = n.transcription_status === "complete" || (n.has_transcript && !n.transcription_pending)
    var summary = n.summary_status === "complete" || (n.summary_status !== "pending" && !!n.summary_preview)
    if (n.source === "manual") return [
        {label: "Áudio", state: "removed"}, {label: "Transcrição", state: "removed"},
        {label: "Resumo", state: summary ? "done" : "waiting"}, {label: "Somente local", state: "removed"}
    ]
    var saved = z.status === "ok" && z.facts_status !== "pending_lineage"
    return [
        {label: "Áudio", state: n.recordings_count > 0 ? "done" : transcript ? "removed" : "waiting"},
        {label: "Transcrição", state: transcript ? "done" : "waiting"},
        {label: "Resumo", state: summary ? "done" : "waiting"},
        {label: "Zinom", state: saved ? "done" : z.status === "error" ? "error" : "waiting"}
    ]
}

function status(note) {
    var n = note || {}, z = n.zinom || {}, steps = stages(n)
    if (n.cleanup_status === "pending" || z.status === "pending_cleanup") return {label: "Limpeza no Zinom pendente", tone: "waiting"}
    if (n.content_status === "empty") return {label: "Sem gravações válidas", tone: "neutral"}
    if (n.content_status === "invalidated") return {label: "Áudios alterados · reprocessamento necessário", tone: "waiting"}
    if (n.content_status === "rebuilding") return {label: "Reprocessando os áudios atuais", tone: "waiting"}
    if (n.source === "manual") return {label: steps[2].state === "done" ? "Resumo local pronto" : "Anotações locais · resumo ao solicitar", tone: steps[2].state === "done" ? "done" : "neutral"}
    if (z.status === "tombstoned") return {label: "Excluída no Zinom", tone: "neutral"}
    if (z.status === "superseded") return {label: "Substituída no Zinom", tone: "neutral"}
    if (n.audio_status === "mic_mudo" || n.audio_status === "sem_audio")
        return {label: n.audio_status === "mic_mudo" ? "Microfone sem áudio na gravação" : "Gravação sem áudio detectado", tone: "error"}
    if (steps[1].state === "done" && steps[2].state === "done" && steps[3].state === "done")
        return {label: "Concluída · disponível no Zinom", tone: "done"}
    if (steps[1].state === "done" && steps[2].state === "done" && z.status === "error")
        return {label: "Entrega precisa de atenção", tone: "error"}
    var reason = String(n.summary_error || n.transcription_pending_reason || "").toLowerCase()
    if (/cota|quota|limite|429/.test(reason)) return {label: "Aguardando cota · retomada automática", tone: "waiting"}
    if (/conex|offline|rede|timeout/.test(reason)) return {label: "Aguardando conexão", tone: "waiting"}
    if (steps[1].state !== "done") return {label: (n.recordings_count > 0 ? "Transcrição pendente · áudio preservado" : "Transcrição pendente"), tone: "waiting"}
    if (steps[2].state !== "done") return {label: "Resumo pendente", tone: "waiting"}
    if (z.status === "error") return {label: "Entrega precisa de atenção", tone: "error"}
    return {label: "Aguardando confirmação do Zinom", tone: "waiting"}
}
