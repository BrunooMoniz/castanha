.pragma library

.import "i18n.js" as I18N

function projectedLastResult(last, notes) {
  if (!last || !last.slug) return last
  for (var i = 0; i < (notes || []).length; i++) {
    var note = notes[i]
    if (note && note.slug === last.slug)
      return Object.assign({}, last, note)
  }
  return last
}

function uploadComplete(note) {
  return !!note && note.transcription_pending === true
    && String(note.transcription_pending_reason || "").indexOf("Transcrição remota em andamento;") >= 0
}

function transcriptionLine(note, lang) {
  if (!note) return ""
  var pending = note.transcription_pending === true || note.transcription_status === "pending"
  if (!pending && (note.has_transcript || note.transcription_status === "complete"))
    return I18N.t("note.transcription_complete", lang || "pt")
  if (!pending && !note.can_retry) return ""
  return I18N.t(uploadComplete(note) ? "note.upload_complete" : "note.transcription_pending", lang || "pt")
}

function summaryLine(note, lang) {
  if (!note || note.summary_status !== "pending") return ""
  var reason = String(note.summary_error || "")
  if (reason.length > 90) reason = reason.substring(0, 89) + "…"
  return I18N.t("delivery.summary_pending", lang || "pt") + (reason ? ": " + reason : "")
}

function processingLine(note, lang) {
  if (!note) return ""
  var transcription = transcriptionLine(note, lang)
  var summary = summaryLine(note, lang)
  return transcription + (transcription && summary ? " · " : "") + summary
}

function zinomNeedsSync(note) {
  if (!note) return false
  var z = note.zinom || {}
  if (z.status === "tombstoned" || z.status === "superseded") return false
  if (z.status === "skipped") {
    var reason = String(z.reason || "").toLowerCase()
    return reason.indexOf("token") >= 0 || reason.indexOf("credencia") >= 0 || reason.indexOf("desligada") >= 0
  }
  return z.status !== "ok"
}

function zinomIcon(note) {
  return zinomNeedsSync(note) ? "󰀦  " : "󰄬  "
}

function zinomLine(result, lang) {
  var L = lang || "pt"
  if (!result) return ""
  var z = result.zinom
  if (!z) return ""
  if (z.status === "tombstoned") return I18N.t("delivery.tombstoned", L)
  if (z.status === "superseded") return I18N.t("delivery.superseded", L)
  if (z.status === "skipped") {
    if (zinomNeedsSync(result)) return I18N.t("delivery.pending", L)
    return I18N.t("delivery.not_sent", L).replace("{reason}", z.reason || I18N.t("delivery.no_reason", L))
  }
  if (z.errors && z.errors.length > 0) {
    // "Erro no remember: HTTP 406..." é linguagem de log, não de produto.
    var motivo = String(z.errors[0]).replace(/^Erro no \w+( para .+?)?: /, "")
    if (motivo.length > 60) motivo = motivo.substring(0, 59) + "…"
    return I18N.t("delivery.failed", L).replace("{reason}", motivo)
  }
  if (z.status === "error") return I18N.t("delivery.unconfirmed", L)
  var delivered = !!z.remember_id || !!(z.remember && z.remember.ok && z.remember.id)
  if (z.facts_status === "pending_lineage" && delivered) return I18N.t("delivery.saved_facts_pending", L)
  if (z.status === "pending") return I18N.t("delivery.pending", L)
  var fatos = z.facts_ingested || 0
  if (fatos > 0) return I18N.t(fatos === 1 ? "delivery.saved_one_fact" : "delivery.saved_many_facts", L).replace("{n}", fatos)
  // O bloco do metadata traz remember_id; o do estado da sessão traz remember.
  if (z.status === "ok" || delivered) return I18N.t("delivery.saved", L)
  return ""
}

function retryLabel(note, lang) {
  return I18N.t(note && note.retry_stage === "summary" ? "btn.retry_summary" : "btn.retry", lang || "pt")
}
