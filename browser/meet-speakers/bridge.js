// Ponte entre a página (sem acesso às APIs da extensão) e o service worker.
(() => {
  const TAG = "castanha-meet-v1";
  let port = null;
  let beats = 0;
  const replay = () => window.postMessage({ [TAG + "-replay"]: true }, location.origin);
  const connect = () => {
    port = chrome.runtime.connect({ name: TAG });
    port.onDisconnect.addListener(() => { port = null; });
    // O service worker pede o mapa de novo quando o host nativo reabre.
    port.onMessage.addListener((msg) => { if (msg && msg.type === "replay") replay(); });
    replay();
  };
  window.addEventListener("message", (ev) => {
    if (ev.source !== window || ev.origin !== location.origin || !ev.data || !ev.data[TAG]) return;
    const msg = ev.data[TAG];
    if (!msg || typeof msg.type !== "string") return;
    if (msg.type === "levels" && (!msg.frames || !msg.frames.length)) {
      // Silêncio: um sinal a cada 10 s basta para o host fechar o arquivo
      // quando a gravação acaba.
      if (port && ++beats % 10) return;
    }
    try { if (!port) connect(); port.postMessage(msg); } catch (e) { port = null; }
  });
})();
