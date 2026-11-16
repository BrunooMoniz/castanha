// Ponte entre a página (sem acesso às APIs da extensão) e o service worker.
(() => {
  const TAG = "castanha-meet-v1";
  let port = null;
  let portCode = null;
  let beats = 0;
  const replay = () => window.postMessage({ [TAG + "-replay"]: true }, location.origin);
  const connect = () => {
    port = chrome.runtime.connect({ name: TAG });
    port.onDisconnect.addListener(() => { port = null; window.postMessage({ [TAG + "-capture"]: false }, location.origin); });
    // O service worker pede o mapa de novo quando o host nativo reabre.
    port.onMessage.addListener((msg) => {
      if (msg && msg.type === "replay") replay();
      // O host diz quando gravar as trilhas de áudio (Castanha gravando e opção ligada).
      if (msg && msg.type === "capture") window.postMessage({ [TAG + "-capture"]: msg.on === true }, location.origin);
    });
    replay();
  };
  window.addEventListener("message", (ev) => {
    if (ev.source !== window || ev.origin !== location.origin || !ev.data || !ev.data[TAG]) return;
    const msg = ev.data[TAG];
    if (!msg || typeof msg.type !== "string") return;
    if (msg.type === "levels" && (!msg.frames || !msg.frames.length) && !(msg.gaps && msg.gaps.length)) {
      // Silêncio: um sinal a cada 10 s basta para o host fechar o arquivo
      // quando a gravação acaba.
      if (port && ++beats % 10) return;
    }
    // Reenvio de outra reunião não reabre nada.
    if (msg.replay && portCode && msg.code && msg.code !== portCode) return;
    // Reunião nova na mesma aba: outra sessão no host (ele recusa código diferente).
    if (port && msg.code && portCode && msg.code !== portCode) { try { port.disconnect(); } catch (e) {} port = null; }
    try { if (!port) { connect(); portCode = msg.code || null; } if (msg.code && !portCode) portCode = msg.code; port.postMessage(msg); } catch (e) { port = null; }
  });
})();
