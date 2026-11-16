.pragma library

function filtered(meetings, query, status) {
  var needle = String(query || "").toLocaleLowerCase()
  return (meetings || []).filter(function(item) {
    return (status === "all" || item.status === status)
      && (!needle || (String(item.title || "") + " " + String(item.when || "")).toLocaleLowerCase().indexOf(needle) >= 0)
  })
}
function clock(seconds) {
  var n = Math.max(0, Math.floor(Number(seconds) || 0))
  var m = Math.floor(n / 60), s = n % 60
  return (m >= 60 ? Math.floor(m / 60) + ":" + String(m % 60).padStart(2, "0") : String(m))
    + ":" + String(s).padStart(2, "0")
}
function date(value) {
  if (!value) return "Data não informada"
  var parsed = new Date(value)
  return isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString(Qt.locale("pt_BR"), "dd MMM yyyy · HH:mm")
}
function plainNotes(value) {
  return String(value || "").replace(/^#{1,6}\s+/gm, "").replace(/\*\*([^*\n]+)\*\*/g, "$1")
    .replace(/^(\s*)[-*]\s+(?=\S)/gm, "$1• ")
}
function localAudio(value) { return typeof value === "string" && value.indexOf("file:///") === 0 }

// Parsing only creates plain text blocks. Fences and HTML remain literal text.
function notesDocument(value, title) {
  var blocks = [], sections = {decisions: [], actions: []}
  var paragraph = [], activeSection = "", firstContent = true, fence = ""
  function flush(literal) {
    if (!paragraph.length) return
    var text = paragraph.join("\n")
    blocks.push({level: 0, text: literal ? text : plainNotes(text)})
    if (activeSection && !literal) sections[activeSection].push(text)
    paragraph = []
  }
  String(value || "").split(/\r?\n/).forEach(function(line) {
    var fenceMatch = line.match(/^\s*(`{3,}|~{3,})/)
    if (fenceMatch && !fence) {
      flush()
      if (line.indexOf(fenceMatch[1], line.indexOf(fenceMatch[1]) + fenceMatch[1].length) >= 0) {
        blocks.push({level: 0, text: line}); firstContent = false; return
      }
      fence = fenceMatch[1][0]
      paragraph.push(line); firstContent = false; return
    }
    if (fenceMatch && fence === fenceMatch[1][0]) {
      paragraph.push(line); flush(true); fence = ""; return
    }
    var heading = !fence && line.match(/^(#{1,6})\s+(.+?)\s*#*$/)
    if (heading) {
      flush()
      var text = plainNotes(heading[2]).trim()
      if (!(firstContent && heading[1].length === 1 && text === String(title || "").trim()))
        blocks.push({level: heading[1].length, text: text})
      var label = text.toLocaleLowerCase().replace(/^[^a-zà-ÿ]+/, "").replace(/\s*[:：]\s*$/, "")
      activeSection = /^decis(?:ões|oes)(?: tomadas)?$/.test(label) ? "decisions"
        : /^(pr(?:ó|o)ximos passos|itens de a(?:ç|c)(?:ã|a)o|a(?:ç|c)(?:ões|oes)|tarefas|action items)$/.test(label) ? "actions" : ""
      firstContent = false
    } else if (!line.trim() && !fence) flush()
    else { paragraph.push(line); if (line.trim()) firstContent = false }
  })
  flush(Boolean(fence))
  return {blocks: blocks, sections: sections}
}
function extraFacts(items, document, section) {
  function normalized(line) {
    return plainNotes(line).replace(/^\s*(?:[-*•□]|\d+[.)])\s+/, "").trim()
  }
  var existing = []
  ;(document.sections[section] || []).forEach(function(block) {
    block.split("\n").forEach(function(line) { existing.push(normalized(line)) })
  })
  // Only a complete identical line suppresses a repeated extracted item.
  // Different wording, numbers, assignments and deadlines stay visible.
  return (items || []).filter(function(item) { return existing.indexOf(normalized(item)) < 0 })
}

// "2026-09-12T11:29:50.281638" -> "2026-09-12". Sem data, vazio: nunca inventa um dia.
function isoDate(value) {
  var text = String(value || "")
  var match = text.match(/^(\d{4}-\d{2}-\d{2})/)
  if (!match) return ""
  // Com fuso explícito, o dia é o do relógio local naquele instante; sem fuso, já é local.
  if (!/(Z|[+-]\d{2}:?\d{2})$/.test(text)) return match[1]
  var parsed = new Date(text)
  if (isNaN(parsed.getTime())) return match[1]
  return parsed.getFullYear() + "-" + String(parsed.getMonth() + 1).padStart(2, "0") + "-" + String(parsed.getDate()).padStart(2, "0")
}
// Rótulo de um evento da agenda para o seletor: hora, título, conta e convidados.
function eventLabel(event) {
  // O evento vem com o fuso do calendário; a hora mostrada é a do relógio da máquina.
  var parsed = new Date(String(event && event.start || ""))
  var time = isNaN(parsed.getTime()) ? "--:--"
    : String(parsed.getHours()).padStart(2, "0") + ":" + String(parsed.getMinutes()).padStart(2, "0")
  var guests = event && Array.isArray(event.attendees) ? event.attendees.length : 0
  return time + " · " + String(event && event.title || "Reunião")
    + (event && event.account ? " · " + String(event.account).split("@")[0] : "")
    + (guests ? " · " + guests + (guests === 1 ? " convidado" : " convidados") : "")
}
// Destinos para mover uma gravação: as reuniões do mesmo dia primeiro, depois as
// mais recentes, nunca a própria. Limite curto: um seletor com 50 itens não serve.
function moveTargets(meetings, currentSlug, when, limit) {
  var day = isoDate(when)
  var others = (meetings || []).filter(function(m) { return m && m.slug && m.slug !== currentSlug })
  var sameDay = others.filter(function(m) { return day && isoDate(m.when) === day })
  var rest = others.filter(function(m) { return sameDay.indexOf(m) < 0 })
  return sameDay.concat(rest).slice(0, limit || 20).map(function(m) {
    return {slug: m.slug, title: m.title || m.slug, when: m.when || ""}
  })
}

// O que o agente recebe desta reunião: {audience, auto} ou null.
function shareState(agent, slug) {
  var shared = agent && agent.shared
  return shared && slug && Object.prototype.hasOwnProperty.call(shared, slug) ? shared[slug] : null
}
// Linha de estado do compartilhamento: quem recebe, para quem vale e se é por regra do agente.
function shareLine(agent, state, hasNotes) {
  var name = String(agent && agent.name || "Agente")
  if (agent && agent.error) return name + ": " + agent.error
  if (!state) return hasNotes ? name + " não recebe esta reunião" : name + ": sem notas ainda, nada a compartilhar"
  var label = ""
  ;(agent.audiences || []).forEach(function(a) { if (a.id === state.audience) label = a.label })
  return name + " recebe esta reunião" + (label ? " · " + label : "") + (state.auto ? " · automático" : "")
}

// "46 min", "1 h 12 min": duração para ler, não para cronometrar.
function duration(seconds) {
  var m = Math.round(Math.max(0, Number(seconds) || 0) / 60)
  if (m < 1) return "menos de 1 min"
  return m < 60 ? m + " min" : Math.floor(m / 60) + " h" + (m % 60 ? " " + (m % 60) + " min" : "")
}
// Dia da reunião para o cabeçalho de grupo do histórico: Hoje, Ontem ou a data.
function dayLabel(when, now) {
  var day = isoDate(when)
  if (!day) return "Sem data"
  var today = now || new Date()
  function iso(d) { return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0") }
  if (day === iso(today)) return "Hoje"
  if (day === iso(new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1))) return "Ontem"
  var parts = day.split("-"), parsed = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]))
  return parsed.toLocaleDateString(Qt.locale("pt_BR"), Number(parts[0]) === today.getFullYear() ? "ddd, d 'de' MMM" : "ddd, d 'de' MMM 'de' yyyy")
}
function timeOfDay(when) {
  var parsed = new Date(String(when || ""))
  return isNaN(parsed.getTime()) ? "" : String(parsed.getHours()).padStart(2, "0") + ":" + String(parsed.getMinutes()).padStart(2, "0")
}
// Histórico em linhas: um cabeçalho por dia e, abaixo, as reuniões daquele dia (na ordem recebida).
function historyRows(meetings, now) {
  var rows = [], last = null
  ;(meetings || []).forEach(function(m) {
    var day = isoDate(m.when)
    if (!rows.length || day !== last) { rows.push({header: dayLabel(m.when, now), meeting: null}); last = day }
    rows.push({header: "", meeting: m})
  })
  return rows
}
// Segunda linha de uma reunião no histórico: hora, duração, o que falta e quem recebe.
function historyMeta(meeting, agents) {
  var parts = [timeOfDay(meeting.when), meeting.duration_seconds > 0 ? duration(meeting.duration_seconds) : ""]
  if (meeting.status !== "complete") parts.push(meeting.status_label)
  var names = (agents || []).filter(function(a) { return shareState(a, meeting.slug) }).map(function(a) { return a.name })
  if (names.length) parts.push("com " + names.join(", "))
  return parts.filter(function(x) { return x }).join(" · ")
}
// Falas: segmentos seguidos da mesma pessoa viram um bloco só. Quebra por pessoa, por gravação, por pausa
// longa e por tamanho, para o tempo do bloco continuar levando ao trecho certo do áudio.
function turns(segments, query) {
  var out = [], last = null
  ;(segments || []).forEach(function(s) {
    var timed = last && s.start !== null && last.end !== null
    var gap = timed ? s.start - last.end : 0
    if (!last || last.channel !== s.channel || last.audio_index !== s.audio_index || (last.start === null) !== (s.start === null)
        || gap > 8 || gap < -1 || last.text.length > 600) {
      last = {channel: s.channel, audio_index: s.audio_index, start: s.start, end: s.end, can_seek: s.can_seek, text: String(s.text)}
      out.push(last)
    } else { last.text += " " + s.text; if (s.end !== null) last.end = s.end }
  })
  var needle = String(query || "").trim().toLocaleLowerCase()
  return !needle ? out : out.filter(function(t) { return (t.channel + " " + t.text).toLocaleLowerCase().indexOf(needle) >= 0 })
}
// Parte da conversa de cada pessoa, da maior para a menor, pelo volume de texto: a duração do segmento
// inclui silêncio e engana. Fala sem dono aparece com o rótulo que tem: não se esconde.
function talkShares(segments) {
  var by = {}, order = [], total = 0
  ;(segments || []).forEach(function(s) {
    var size = String(s.text || "").trim().length
    if (!size) return
    if (!Object.prototype.hasOwnProperty.call(by, s.channel)) { by[s.channel] = 0; order.push(s.channel) }
    by[s.channel] += size; total += size
  })
  return order.map(function(name) { return {name: name, share: by[name] / total} })
    .sort(function(a, b) { return b.share - a.share })
}
// Rótulo de canal, não de pessoa: a gravação não soube dizer quem falou.
function genericSpeaker(name) {
  return ["Microfone local", "Áudio remoto", "Canal não identificado", "Origem não identificada"].indexOf(String(name)) >= 0
}
