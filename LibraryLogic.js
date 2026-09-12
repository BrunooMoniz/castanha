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
  var match = String(value || "").match(/^(\d{4}-\d{2}-\d{2})/)
  return match ? match[1] : ""
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
