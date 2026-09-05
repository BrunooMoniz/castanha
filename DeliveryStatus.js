.pragma library

function zinomLine(result) {
  if (!result) return ""
  var z = result.zinom
  if (!z) return ""
  if (z.status === "tombstoned") return "Excluído no Zinom"
  if (z.status === "skipped") return "Não enviado ao Zinom: " + (z.reason || "sem motivo declarado")
  if (z.errors && z.errors.length > 0) {
    // "Erro no remember: HTTP 406..." é linguagem de log, não de produto.
    var motivo = String(z.errors[0]).replace(/^Erro no \w+( para .+?)?: /, "")
    if (motivo.length > 60) motivo = motivo.substring(0, 59) + "…"
    return "Não salvou no Zinom (" + motivo + ")"
  }
  var delivered = !!z.remember_id || !!(z.remember && z.remember.ok && z.remember.id)
  if (z.facts_status === "pending_lineage" && delivered) return "Nota salva no Zinom; fatos pendentes"
  if (z.status === "pending") return "Envio ao Zinom pendente"
  var fatos = z.facts_ingested || 0
  if (fatos > 0) return "Salvo no Zinom, com " + fatos + (fatos === 1 ? " fato" : " fatos")
  // O bloco do metadata traz remember_id; o do estado da sessão traz remember.
  if (z.status === "ok" || delivered) return "Salvo no Zinom"
  return ""
}
