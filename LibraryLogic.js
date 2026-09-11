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
