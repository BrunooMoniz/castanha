# Quem fala no Google Meet

O canal do sistema junta todos os participantes remotos. Numa chamada do Google
Meet, o próprio navegador sabe quem está falando: o Castanha passa a usar isso
para trocar o rótulo `Áudio do sistema` pelo nome do participante.

## De onde vem o sinal

Não vem dos requests HTTP (a aba Network só mostra sincronização e logs). Vem do
WebRTC, dentro da página:

- canal de dados `collections` (protobuf em gzip): liga cada dispositivo ao nome
  exibido e cada fonte de áudio (SSRC) ao dispositivo;
- `RTCRtpReceiver.getContributingSources()`: nível de áudio de cada fonte, com o
  instante do pacote.

Formato observado numa chamada de teste em 23/09/2026 e não documentado pelo
Google. O decodificador casa pelo formato das submensagens, não pelo caminho
inteiro, e qualquer falha deixa a transcrição com o rótulo do canal.

## Peças

- `browser/meet-speakers/`: extensão MV3. `page.js` roda no mundo da página antes
  do Meet criar a conexão; `bridge.js` e `background.js` levam as mensagens ao
  host nativo. ID fixo pela chave do manifest.
- `bin/castanha-meet-host` → `castanha/meet_speakers.py`: host de native
  messaging. Grava `~/.local/state/castanha/meet/*.jsonl` (0600) SÓ enquanto o
  Castanha grava; fora disso guarda o mapa em memória e descarta os níveis.
  Arquivos com mais de 7 dias são apagados.
- No stop, o trecho da chamada que cobre a gravação vai para
  `bronze/<reunião>/.speakers/<sha do áudio>.json`. A atribuição fica em
  `.speakers/<sha>.result.json`; o checkpoint da transcrição não muda. Excluir
  a gravação leva os dois junto (e restaurar traz de volta); mover copia o recorte.

## Atribuição

Cada fala do canal do sistema recebe o participante que dominou o intervalo:
cobertura mínima de 30%, e as outras vozes somadas (inclusive participante
ainda sem nome) em no máximo 15% do intervalo. A fonte de mistura do Meet, que
não pertence a ninguém, é ignorada. Fonte reatribuída no meio da chamada vale
para quem era dono dela em cada instante.

Pausas da gravação são descontadas; fala que atravessa uma pausa fica sem nome,
e a voz de quem falou durante a pausa não conta. O atraso entre gravação e Meet
é achado por correlação com o canal do sistema, em cada trecho entre pausas;
correlação fraca (sessão errada, áudio de outro app) cancela aquele trecho. O microfone nunca recebe nome. Falas que cruzam a troca
de pessoa ficam com o rótulo do canal.

No texto: `[12.30s–15.10s] Ana Souza (Meet): ...`. Nos segmentos:
`attributed_name`, `attribution_source: google_meet_webrtc` e a cobertura,
sem alterar `speaker` e `origin`. O Silver ganha o bloco `speakers` com
presença observada. O filtro do Gold (`_ground_gold`) continua igual.

## Ligar, conferir, desligar

```
castanha meet install     # liga a flag, registra o host e põe a extensão no chromium-flags.conf
castanha meet status      # ✓/✗ de cada peça, inclusive se o Chromium aberto já carregou
castanha meet uninstall   # desfaz as três coisas
```

A extensão só carrega quando o Chromium é aberto de novo. `install` guarda o
`chromium-flags.conf` original em `chromium-flags.conf.pre-castanha-meet`.
`omarchy-refresh-chromium` reescreve esse arquivo: depois dele, rode
`castanha meet install` de novo.

## Reversão

1. `castanha meet uninstall` e reabrir o Chromium.
2. Reuniões já processadas: `castanha retry <slug>` refaz a transcrição com os
   rótulos do canal (a flag desligada ignora `.speakers/`).

Limites: só Google Meet no Chromium; a própria voz não passa pela rede do Meet
(fica no microfone); duas pessoas na mesma sala e no mesmo dispositivo saem com
o nome do dispositivo.
