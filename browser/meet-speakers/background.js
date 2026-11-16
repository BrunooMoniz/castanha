// Um host nativo por aba do Meet: cada chamada vira uma sessão própria no Castanha.
const HOST = "io.github.brunoomoniz.castanha.meet";
chrome.runtime.onConnect.addListener((tab) => {
  if (tab.name !== "castanha-meet-v1") return;
  let native = null;
  let opened = 0;
  const open = () => {
    try {
      native = chrome.runtime.connectNative(HOST);
      native.onDisconnect.addListener(() => { native = null; void chrome.runtime.lastError; try { tab.postMessage({ type: "capture", on: false }); } catch (e) {} });
      // Único pedido do host à página: ligar ou desligar a gravação das trilhas de áudio.
      native.onMessage.addListener((msg) => { if (msg && msg.type === "capture") { try { tab.postMessage({ type: "capture", on: msg.on === true }); } catch (e) {} } });
      // Host novo começa com o mapa vazio: a página reenvia o que já sabe.
      if (opened++) tab.postMessage({ type: "replay" });
    } catch (e) { native = null; }
  };
  tab.onMessage.addListener((msg) => {
    if (!native) open();
    try { if (native) native.postMessage(msg); } catch (e) { native = null; }
  });
  tab.onDisconnect.addListener(() => {
    try { if (native) native.postMessage({ type: "bye", t: Date.now() }); native && native.disconnect(); } catch (e) {}
    native = null;
  });
});
