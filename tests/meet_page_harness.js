// Roda browser/meet-speakers/page.js num Teams simulado (sem navegador) e
// devolve, em JSON, as mensagens que a página mandaria para a extensão.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const posted = [];
let thread = "19:meeting_ABCdef123@thread.v2";
const timers = [];
let now = 1790000000000;
let participants = [
  { id: "8:orgid:ana", displayName: "Ana Teste", hasAudioSource: (s) => s === 414 },
  { id: "8:orgid:beto", displayName: "Beto Teste", hasAudioSource: (s) => s === 415 },
];
let speaking = 414;
const receiver = {
  track: { kind: "audio" },
  getContributingSources: () => [{ source: speaking, timestamp: now }],  // sem audioLevel, como o Teams pode mandar
};
class FakePC { constructor() { this.connectionState = "connected"; } addEventListener() {} getReceivers() { return [receiver]; } }
const window = {
  RTCPeerConnection: FakePC,
  location: { hostname: "teams.microsoft.com", origin: "https://teams.microsoft.com", pathname: "/v2/",
              href: "https://teams.microsoft.com/v2/?meetingjoin=true#/l/meetup-join/19%3ameeting_ABCdef123%40thread.v2/0" },
  msteamscalling: { deref: () => ({ callingService: { getActiveCall: () => ({ threadId: thread, participants }) } }) },
  postMessage: (msg) => posted.push(msg["castanha-meet-v1"]),
  addEventListener: () => {},
};
const context = {
  window, location: window.location, performance: { timeOrigin: now },
  Date: { now: () => now }, btoa: (s) => Buffer.from(s, "binary").toString("base64"),
  setInterval: (fn, ms) => timers.push({ fn, ms, next: now + ms }),
  Map, Set, Math, String, Uint8Array, decodeURIComponent,
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname, "../browser/meet-speakers/page.js"), "utf8"), context);
new window.RTCPeerConnection();
const run = (until) => {
  while (true) {
    const t = timers.reduce((a, b) => (a.next <= b.next ? a : b));
    if (t.next > until) { now = until; return; }
    now = t.next; t.fn(); t.next += t.ms;
  }
};
run(now + 3000);
speaking = 415;
run(now + 3000);
// Beto sai e a fonte 415 segue chegando sem dono: o vínculo é revogado.
participants = participants.filter((p) => p.id !== "8:orgid:beto");
run(now + 2000);
// Lista de participantes quebra: tudo revogado.
const saved = participants;
participants = { [Symbol.iterator]() { throw new Error("indisponível"); } };
run(now + 1000);
participants = saved;
// Outra reunião na mesma aba, sem recarregar.
thread = "19:meeting_OUTRAreuniao@thread.v2";
participants = [{ id: "8:orgid:carla", displayName: "Carla Teste", hasAudioSource: (s) => s === 500 }];
speaking = 500;
run(now + 3000);
process.stdout.write(JSON.stringify(posted));
