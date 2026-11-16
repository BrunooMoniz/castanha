// Roda browser/meet-speakers/page.js num Meet simulado (sem navegador), com a
// tela mostrando os participantes, e devolve em JSON o que a página mandaria.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const posted = [];
const timers = [];
let now = 1790000000000;
const DEV = (n) => `spaces/FakeSpace01/devices/${n}`;
// Elemento falso: só o que o page.js consulta.
const el = (attrs, children = [], text = "") => ({
  getAttribute: (k) => (k in attrs ? attrs[k] : null),
  textContent: text,
  querySelectorAll: (sel) => {
    const want = sel.split(",").map((s) => s.trim());
    const all = [];
    const walk = (node) => {
      for (const c of node.children) {
        if (want.some((w) => (w === "[data-self-name]" && "data-self-name" in c.attrs) || (w === ".notranslate" && (c.attrs.class || "").split(" ").includes("notranslate")))) all.push(c);
        walk(c);
      }
    };
    walk({ children });
    return all;
  },
  attrs, children, tagName: attrs.tag || "SPAN",
});
// Quadro de vídeo: o nome fica num span "notranslate". O próprio usuário aparece como "Você".
let tiles = [
  // O ícone do alfinete vem antes do nome e também é "notranslate": o texto dele é o nome do ícone.
  el({ "data-participant-id": DEV(204) }, [el({ class: "google-symbols notranslate", tag: "I" }, [], "keep_outline"),
                                           el({ class: "notranslate" }, [], "frame_person"), el({ class: "notranslate" }, [], "Ana Teste")]),
  // Quadro só com ícones (nome escondido): fica sem nome, não com o nome do ícone.
  el({ "data-participant-id": DEV(207) }, [el({ class: "notranslate" }, [], "keep_outline"), el({ class: "material-icons notranslate" }, [], "keep")]),
  el({ "data-participant-id": DEV(205) }, [el({ "data-self-name": "Você" }, [], "Você")]),
  el({ "data-participant-id": "nao-e-dispositivo" }, [el({ class: "notranslate" }, [], "Intruso")]),
];
class FakePC { constructor() { this.connectionState = "connected"; } addEventListener() {} getReceivers() { return []; } }
const window = {
  RTCPeerConnection: FakePC,
  location: { hostname: "meet.google.com", origin: "https://meet.google.com", pathname: "/abc-defg-hij", href: "https://meet.google.com/abc-defg-hij" },
  postMessage: (msg) => posted.push(msg["castanha-meet-v1"]),
  addEventListener: () => {},
};
const document = { querySelectorAll: (sel) => (sel === "[data-participant-id]" ? tiles : []) };
const context = {
  window, document, location: window.location, performance: { timeOrigin: now },
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
run(now + 5000);
// Beto aparece na lista de pessoas (nome no aria-label do item) e nada mais muda.
tiles = tiles.concat([el({ "data-participant-id": DEV(206), role: "listitem", "aria-label": "Beto Teste" })]);
run(now + 5000);
process.stdout.write(JSON.stringify(posted.filter((m) => m.type === "screen")));
