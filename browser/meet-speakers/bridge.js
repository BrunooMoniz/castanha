// Ponte entre a página (sem acesso às APIs da extensão) e o service worker.
(() => {
  const TAG = "castanha-meet-v1";
  let port = null;
  const connect = () => {
    port = chrome.runtime.connect({ name: TAG });
    port.onDisconnect.addListener(() => { port = null; });
  };
  window.addEventListener("message", (ev) => {
    if (ev.source !== window || ev.origin !== location.origin || !ev.data || !ev.data[TAG]) return;
    const msg = ev.data[TAG];
    if (!msg || typeof msg.type !== "string") return;
    if (msg.type === "levels" && (!msg.frames || !msg.frames.length)) {
      // Heartbeat só vale para abrir a sessão; sem fala, não há o que enviar.
      if (port) return;
    }
    try { if (!port) connect(); port.postMessage(msg); } catch (e) { port = null; }
  });
})();
