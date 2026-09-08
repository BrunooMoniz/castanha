.pragma library

var LEVELS = "▁▂▃▄▅▆▇█"
var HISTORY_SIZE = 5

function clampPeak(value) {
  var peak = Number(value)
  if (!isFinite(peak) || peak <= 0) return 0
  return Math.min(1, peak)
}

function isFresh(updatedAt, nowMs, recording) {
  if (!recording) return false
  var timestamp = Number(updatedAt)
  var now = Number(nowMs)
  var age = now / 1000 - timestamp
  return isFinite(timestamp) && timestamp > 0 && isFinite(now)
    && age >= 0 && age <= 1.5
}

function combinedPeak(micPeak, systemPeak, mode, micMuted) {
  var mic = micMuted ? 0 : clampPeak(micPeak)
  if (mode !== "dual") return mic
  return Math.max(mic, clampPeak(systemPeak))
}

function levelChar(value) {
  var peak = clampPeak(value)
  return LEVELS.charAt(Math.round(peak * (LEVELS.length - 1)))
}

function pushSample(samples, peak) {
  var next = Array.isArray(samples) ? samples.slice(-HISTORY_SIZE + 1) : []
  while (next.length < HISTORY_SIZE - 1) next.unshift(0)
  next.push(clampPeak(peak))
  return next
}

function render(samples) {
  var values = Array.isArray(samples) ? samples.slice(-HISTORY_SIZE) : []
  while (values.length < HISTORY_SIZE) values.unshift(0)
  return values.map(levelChar).join("")
}
