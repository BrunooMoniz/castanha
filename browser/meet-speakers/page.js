// Roda no mundo da página, antes do Meet criar a conexão WebRTC: é o único
// jeito de enxergar o canal "collections" (quem é quem) e o nível de áudio de
// cada fonte (quem fala). Nada sai daqui além de postMessage para bridge.js.
(() => {
  if (window.__castanhaMeet) return;
  window.__castanhaMeet = true;
  const TAG = "castanha-meet-v1";
  const TEAMS = /(^|\.)teams\.(microsoft\.com|live\.com|cloud\.microsoft)$/.test(location.hostname);
  // Teams: a reunião é o thread "19:meeting_...@thread.v2", na URL ou na chamada ativa.
  const hash = (text) => { let h = 5381; for (const ch of text) h = ((h * 33) ^ ch.charCodeAt(0)) >>> 0; return h.toString(16).padStart(8, "0"); };
  const teamsCall = () => {
    try {
      if (window.callingDebug && window.callingDebug.observableCall) return window.callingDebug.observableCall;
      const calling = window.msteamscalling && window.msteamscalling.deref && window.msteamscalling.deref();
      return calling && calling.callingService && calling.callingService.getActiveCall && calling.callingService.getActiveCall();
    } catch (e) { return null; }
  };
  const teamsThread = () => {
    const call = teamsCall();
    if (call && typeof call.threadId === "string") return call.threadId;
    const m = decodeURIComponent(location.href).match(/19:meeting_[A-Za-z0-9_-]+@thread\.v2/);
    return m ? m[0] : null;
  };
  const code = () => {
    if (!TEAMS) return (location.pathname.match(/^\/([a-z]{3,}-[a-z]{3,}-[a-z]{3,})/) || [])[1] || null;
    const thread = teamsThread();
    return thread && teamsCall() ? "teams-" + hash(thread) : null;
  };
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
    const current = code();
    for (const msg of history) if (msg.code === current) post({ ...msg, replay: true });
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
  const PRESENT_LEVEL = 0.2;
  let presenceOnly = true;  // o lote só tem presença (Teams sem audioLevel)
  const teamsSources = new Set();
  // Teams não tem o canal "collections": a própria página mantém a lista de
  // participantes, e cada um responde se uma fonte de áudio (CSRC) é dele.
  const teamsKnown = new Map();
  const teamsOwner = new Map();
  const teamsKnownSource = new Set();
  // Outra reunião na mesma aba (o Teams troca sem recarregar): tudo recomeça.
  let currentCode = null;
  const resetIfNewCall = (c) => {
    if (!c || c === currentCode) return;
    if (currentCode !== null) {
      history.length = 0; overflow = false;
      teamsKnown.clear(); teamsOwner.clear(); teamsKnownSource.clear(); teamsSources.clear(); seen.clear(); frames = []; gaps = [];
      shown.clear(); lastProbe = "";
      stopRecorders(); trackIndex.clear(); trackFrames = [];
    }
    currentCode = c;
  };
  if (TEAMS) setInterval(() => {
    const c = code();
    const call = teamsCall();
    // Lista de participantes indisponível: os vínculos antigos não valem mais.
    const revokeAll = () => {
      if (!teamsOwner.size || !currentCode) return;
      const sources = [...teamsOwner.keys()].map((csrc) => [csrc, null]);
      teamsOwner.clear();
      const msg = { type: "mapping", code: currentCode, t: Date.now(), devices: [], sources };
      if (history.length < 20000) history.push(msg); else overflow = true;
      post(msg);
    };
    if (!c || !call) { revokeAll(); return; }
    resetIfNewCall(c);
    const people = [];
    try {
      for (const p of call.participants || []) if (p && p.id && p.displayName) people.push(p);
    } catch (e) { revokeAll(); return; }
    const devices = [], sources = [];
    for (const p of people) {
      if (teamsKnown.get(p.id) !== p.displayName) { teamsKnown.set(p.id, p.displayName); devices.push([p.id, String(p.displayName)]); }
    }
    for (const csrc of teamsSources) {
      let owner = null;
      for (const p of people) { try { if (p.hasAudioSource && p.hasAudioSource(csrc)) { owner = p.id; break; } } catch (e) {} }
      // Ninguém reconhece a fonte (saiu, reconectou, lista indisponível): o
      // vínculo antigo é revogado para não herdar outra voz.
      if ((teamsOwner.get(csrc) || null) !== owner) {
        if (owner) teamsOwner.set(csrc, owner); else teamsOwner.delete(csrc);
        if (owner || teamsKnownSource.has(csrc)) sources.push([csrc, owner]);
        if (owner) teamsKnownSource.add(csrc);
      }
    }
    if (devices.length || sources.length) {
      const msg = { type: "mapping", code: c, t: Date.now(), devices, sources };
      if (history.length < 20000) history.push(msg); else overflow = true;
      post(msg);
    }
  }, 500);
  // Meet: o nome que aparece na tela. O canal "collections" só anuncia o nome
  // quando alguém entra, sai ou muda, então quem já estava na chamada fica sem
  // nome. A página mostra o nome em cada quadro de vídeo e na lista de pessoas,
  // preso ao mesmo identificador de dispositivo que o canal usa.
  const DEVICE = /^spaces\/[\w-]+\/devices\/\d+$/;
  const shown = new Map();
  let lastProbe = "";
  const screenName = (el) => {
    // O próprio usuário aparece como "Você"/"You" (valor de data-self-name): não é nome.
    const self = new Set();
    for (const n of el.querySelectorAll("[data-self-name]")) self.add((n.getAttribute("data-self-name") || "").trim());
    const tries = [];
    if (el.getAttribute("role") === "listitem") tries.push(el.getAttribute("aria-label"));
    // Ícone do quadro também é "notranslate", e o texto dele é o nome do ícone (keep_outline, frame_person).
    const icon = (n) => n.tagName === "I" || /symbols|icons/i.test(n.getAttribute("class") || "");
    for (const n of el.querySelectorAll("[data-self-name], .notranslate")) if (!icon(n)) tries.push(n.textContent);
    for (const raw of tries) {
      const text = (raw || "").replace(/\s+/g, " ").trim();
      if (text && text.length <= 120 && !self.has(text) && !/^[a-z0-9]+(_[a-z0-9]+)+$/.test(text)) return text;
    }
    return null;
  };
  if (!TEAMS && typeof document !== "undefined") setInterval(() => {
    const c = code();
    if (!c) return;
    resetIfNewCall(c);
    let nodes = [];
    try { nodes = document.querySelectorAll("[data-participant-id]"); } catch (e) { return; }
    const found = new Map();
    for (const el of nodes) {
      const id = el.getAttribute("data-participant-id") || "";
      if (!DEVICE.test(id) || found.get(id)) continue;
      let name = null;
      try { name = screenName(el); } catch (e) {}
      found.set(id, name);
    }
    const devices = [];
    let named = 0;
    for (const [id, name] of found) {
      if (!name) continue;
      named++;
      if (shown.get(id) !== name) { shown.set(id, name); devices.push([id, name]); }
    }
    // A sonda (quantos elementos, quantos dispositivos, quantos com nome) diz ao
    // host se a tela do Meet mudou de formato e os nomes pararam de vir.
    const probe = nodes.length + "/" + found.size + "/" + named;
    if (!devices.length && probe === lastProbe) return;
    lastProbe = probe;
    const msg = { type: "screen", code: c, t: Date.now(), devices, probe: { nodes: nodes.length, devices: found.size, named } };
    if (devices.length) { if (history.length < 20000) history.push(msg); else overflow = true; }
    post(msg);
  }, 2000);
  // Trilhas separadas: o Meet entrega poucas trilhas de áudio (as vozes mais
  // altas), cada uma com uma voz por vez. Gravando cada trilha aqui, com o
  // relógio da própria página, o dono de cada trecho sai da trilha, sem alinhar
  // dois relógios. Só grava quando o host pede (Castanha gravando e a opção
  // `meet.track_audio` ligada) e para assim que ele manda parar.
  let capture = false;
  const trackIndex = new Map();   // track.id -> número da trilha nesta chamada
  const recorders = new Map();    // track.id -> MediaRecorder (ou null se não deu)
  const sinks = new Map();        // track.id -> elemento de áudio mudo que puxa a trilha
  let trackFrames = [];
  const stopRecorders = () => {
    for (const rec of recorders.values()) { try { if (rec && rec.state !== "inactive") rec.stop(); } catch (e) {} }
    for (const el of sinks.values()) { try { el.pause(); el.srcObject = null; } catch (e) {} }
    recorders.clear(); sinks.clear();
  };
  const startRecorder = (track, index) => {
    try {
      const stream = new MediaStream([track]);
      // O Chromium só entrega áudio de trilha remota ao gravador se algum destino a estiver puxando
      // (medido: sem isso o gravador devolve zero bytes). Um elemento mudo, fora da página, basta.
      if (!sinks.has(track.id) && typeof Audio === "function") {
        const el = new Audio();
        el.muted = true; el.srcObject = stream;
        const playing = el.play();
        if (playing && playing.catch) playing.catch(() => {});
        sinks.set(track.id, el);
      }
      const rec = new MediaRecorder(stream, { mimeType: "audio/webm;codecs=opus", audioBitsPerSecond: 32000 });
      const started = Date.now();
      let seq = 0;
      rec.ondataavailable = (ev) => {
        if (!ev.data || !ev.data.size) return;
        const n = seq++;
        ev.data.arrayBuffer().then((buf) => post({ type: "audio", code: code(), t: Date.now(), track: index, started, seq: n, b64: toB64(buf) })).catch(() => {});
      };
      rec.onerror = () => { recorders.set(track.id, null); };
      rec.start(2000);
      recorders.set(track.id, rec);
    } catch (e) { recorders.set(track.id, null); }  // sem suporte: não insiste nesta trilha
  };
  window.addEventListener("message", (ev) => {
    if (ev.source !== window || !ev.data || typeof ev.data[TAG + "-capture"] !== "boolean") return;
    capture = ev.data[TAG + "-capture"];
    if (!capture) stopRecorders();
  });
  let frames = [];
  // Travada da página: sem consulta, falas de outra pessoa podem sumir. O
  // host recebe o buraco e não atribui nome às falas que caem nele. Acima de
  // 1,5 s: aba em segundo plano e sem som tem o timer reduzido a 1 s.
  let gaps = [];
  let lastPoll = Date.now();
  const epoch = (ts) => (ts > 1e12 ? ts : performance.timeOrigin + ts);
  setInterval(() => {
    const nowPoll = Date.now();
    if (nowPoll - lastPoll > 1500 && pcs.length) gaps.push([lastPoll, nowPoll]);
    lastPoll = nowPoll;
    for (let i = pcs.length - 1; i >= 0; i--) {
      const pc = pcs[i];
      if (pc.connectionState === "closed") { pcs.splice(i, 1); continue; }
      let receivers = [];
      try { receivers = pc.getReceivers(); } catch (e) { continue; }
      for (const r of receivers) {
        if (!r.track || r.track.kind !== "audio") continue;
        if (!TEAMS && !trackIndex.has(r.track.id)) trackIndex.set(r.track.id, trackIndex.size);
        const index = trackIndex.get(r.track.id);
        if (capture && index !== undefined && r.track.readyState === "live") {
          const rec = recorders.get(r.track.id);
          // Trilha nova, ou gravador que parou sozinho (trilha renegociada): começa outro arquivo.
          if (rec === undefined || (rec && rec.state === "inactive")) startRecorder(r.track, index);
        }
        let sources = [];
        try { sources = r.getContributingSources(); } catch (e) { continue; }
        for (const s of sources) {
          // Teams pode não mandar o nível: fonte presente no pacote = falando.
          const measured = typeof s.audioLevel === "number";
          if (measured) presenceOnly = false;
          const level = measured ? s.audioLevel : (TEAMS ? PRESENT_LEVEL : -1);
          if (level < 0.01) continue;
          const ts = Math.round(epoch(s.timestamp));
          if (seen.get(s.source) === ts) continue;
          seen.set(s.source, ts);
          if (TEAMS) teamsSources.add(s.source);
          frames.push([ts, s.source, Math.round(level * 1000) / 1000]);
          // A mesma medida, com a trilha em que a voz chegou: é o que liga trilha a pessoa.
          if (index !== undefined) trackFrames.push([ts, index, s.source, Math.round(level * 1000) / 1000]);
        }
      }
    }
  }, 100);
  setInterval(() => {
    const c = code();
    if (!c) { frames = []; trackFrames = []; return; }
    resetIfNewCall(c);
    post({ type: "levels", code: c, t: Date.now(), frames, gaps, presence: presenceOnly && frames.length > 0, tracks: trackFrames });
    presenceOnly = true;
    frames = [];
    trackFrames = [];
    gaps = [];
  }, 1000);
  window.addEventListener("pagehide", () => { stopRecorders(); post({ type: "bye", code: code(), t: Date.now() }); });
})();
