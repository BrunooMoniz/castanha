// Roda no mundo da página, antes do Meet criar a conexão WebRTC: é o único
// jeito de enxergar o canal "collections" (quem é quem) e o nível de áudio de
// cada fonte (quem fala). Nada sai daqui além de postMessage para bridge.js.
(() => {
  if (window.__castanhaMeet) return;
  window.__castanhaMeet = true;
  const TAG = "castanha-meet-v1";
  const code = () => (location.pathname.match(/^\/([a-z]{3,}-[a-z]{3,}-[a-z]{3,})/) || [])[1] || null;
  const post = (msg) => window.postMessage({ [TAG]: msg }, location.origin);
  const toB64 = (buf) => {
    const u = new Uint8Array(buf); let s = "";
    for (let i = 0; i < u.length; i += 0x8000) s += String.fromCharCode.apply(null, u.subarray(i, i + 0x8000));
    return btoa(s);
  };
  const pcs = [];
  // O mapa só chega quando alguém entra ou muda. Se o host nativo reiniciar,
  // ele precisa receber tudo de novo: guardamos as mensagens e reenviamos.
  const history = [];
  let overflow = false;
  window.addEventListener("message", (ev) => {
    if (ev.source !== window || !ev.data || ev.data[TAG + "-replay"] !== true) return;
    for (const msg of history) post({ ...msg, replay: true });
    // Histórico cortado: o host marca a janela e não atribui com mapa velho.
    if (overflow) post({ type: "replay_incomplete", code: code(), t: Date.now() });
  });
  const watch = (channel) => {
    if (channel.label !== "collections") return;
    channel.binaryType = "arraybuffer";
    channel.addEventListener("message", (ev) => {
      const t = Date.now();
      const send = (buf) => {
        const msg = { type: "collections", code: code(), t, b64: toB64(buf) };
        if (history.length < 20000) history.push(msg); else overflow = true;
        post(msg);
      };
      if (ev.data instanceof ArrayBuffer) send(ev.data);
      else if (ev.data && ev.data.arrayBuffer) ev.data.arrayBuffer().then(send);
    });
  };
  const Original = window.RTCPeerConnection;
  if (!Original) return;
  function Wrapped(...args) {
    const pc = new Original(...args);
    pcs.push(pc);
    pc.addEventListener("datachannel", (ev) => watch(ev.channel));
    return pc;
  }
  Wrapped.prototype = Original.prototype;
  Object.setPrototypeOf(Wrapped, Original);
  window.RTCPeerConnection = Wrapped;

  // Cada fonte contribuinte (CSRC) traz o nível do último pacote e o instante
  // em que ele chegou. O instante do pacote vale mais que o da consulta.
  const seen = new Map();
  let frames = [];
  const epoch = (ts) => (ts > 1e12 ? ts : performance.timeOrigin + ts);
  setInterval(() => {
    for (let i = pcs.length - 1; i >= 0; i--) {
      const pc = pcs[i];
      if (pc.connectionState === "closed") { pcs.splice(i, 1); continue; }
      let receivers = [];
      try { receivers = pc.getReceivers(); } catch (e) { continue; }
      for (const r of receivers) {
        if (!r.track || r.track.kind !== "audio") continue;
        let sources = [];
        try { sources = r.getContributingSources(); } catch (e) { continue; }
        for (const s of sources) {
          if (typeof s.audioLevel !== "number" || s.audioLevel < 0.01) continue;
          const ts = Math.round(epoch(s.timestamp));
          if (seen.get(s.source) === ts) continue;
          seen.set(s.source, ts);
          frames.push([ts, s.source, Math.round(s.audioLevel * 1000) / 1000]);
        }
      }
    }
  }, 100);
  setInterval(() => {
    const c = code();
    if (!c) { frames = []; return; }
    post({ type: "levels", code: c, t: Date.now(), frames });
    frames = [];
  }, 1000);
  window.addEventListener("pagehide", () => post({ type: "bye", code: code(), t: Date.now() }));
})();
