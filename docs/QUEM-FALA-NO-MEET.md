# Quem fala no Google Meet e no Microsoft Teams

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

Regra da versão `meet-speakers-v2` (nov/2026), calibrada em 25 reuniões reais de set e
out/2026 (12.345 falas do canal do sistema). A versão anterior nomeava 9% delas; esta
nomeia 58% e identifica outros 29% como participante numerado.

- **Uma voz por fonte.** Cada fonte de áudio (CSRC) é uma pessoa. A fala vai para a voz
  que concentrou a energia do intervalo (85% ou mais), desde que nenhuma outra pessoa
  tenha tido voz por mais de 15% do intervalo ou 1 s. Em 81% das falas reais uma voz só
  responde por mais de 99% da energia, então o limiar alto custa pouco.
- **O nome vale para a chamada inteira.** O Meet só anuncia nome e dono da fonte quando
  alguém entra, sai ou muda; é comum o nome chegar quando a pessoa sai. Fonte que teve um
  dono só é dele desde o primeiro pacote, e dispositivo que teve um nome só se chama
  assim o tempo todo. Com mais de um dono ou nome, vale o de cada instante.
- **Sem nome não é sem dono.** Participante que o Meet nunca nomeou sai como
  `Participante 1`, `Participante 2`... (sempre a mesma pessoa naquela reunião), com
  `attribution_named: false` nos segmentos e `named: false` no Silver. Nunca recebe nome
  do convite nem nome citado na conversa.
- **Nome da tela.** `page.js` lê, a cada 2 s, o nome mostrado em cada quadro de vídeo e
  na lista de pessoas (elementos com `data-participant-id`, o mesmo identificador de
  dispositivo do canal) e manda como mensagem `screen`. Cobre quem já estava na chamada,
  que o canal nunca anuncia. O nome do canal, quando existe, vale mais. A sonda
  (`probe`: elementos, dispositivos, com nome) fica no arquivo da sessão para mostrar se
  a tela do Meet mudou de formato. **Ainda não validado numa chamada real.**
- **Mistura do Meet.** A fonte 42 sem dono (assim em 23 chamadas) e qualquer fonte sem
  dono que só espelha as outras não são gente.
- **Atraso por janela.** O atraso entre gravação e Meet deriva (medido: de -0,3 a -1,1 s
  em 40 min). Cada janela de 2 min acha o seu por correlação com o canal do sistema;
  janela sem correlação herda da vizinha, e trecho em que as janelas não concordam fica
  sem atribuição. Platô de correlação vale pelo meio; dois picos separados, não.
- **Continua sem dono:** fala que atravessa uma pausa, que cai numa travada da página,
  dividida entre duas vozes, ou que o canal gravado não acompanhou (Meet mudo, outro som).
- **Microfone.** Com `meet.owner_name` configurado, numa chamada em que o Meet
  identificou alguém, as falas do microfone saem como `Nome (microfone)`
  (`attribution_source: local_microphone_owner`). Sem a opção, seguem `Microfone local`.
- **Captura ausente.** Chamada do Meet gravada sem nenhum sinal da extensão gera um aviso
  na tela, uma vez, e o marcador `.speakers/<sha>.missing.json`.

No texto: `[12.30s–15.10s] Ana Souza (Meet): ...`. Nos segmentos:
`attributed_name`, `attribution_source` e a fatia de energia da voz, sem alterar
`speaker` e `origin`. O Silver ganha o bloco `speakers` com presença observada. O filtro
do Gold (`_ground_gold`) continua igual. O resultado por gravação
(`.speakers/<sha>.result.json`) traz `attributed`, `named` e a lista de participantes.

Bancada usada na calibragem: reexecutar a atribuição sobre `bronze/*/.speakers/*.json` e
os `.jobs/*.json` de cada reunião, com o envelope do canal 1 do áudio.

## Microsoft Teams (no navegador)

A mesma extensão roda em `teams.microsoft.com`, `teams.live.com` e
`teams.cloud.microsoft`. O Teams não tem o canal `collections`: a própria página
mantém a chamada ativa (`msteamscalling` / `callingDebug.observableCall`) com os
participantes, e cada participante responde `hasAudioSource(csrc)` para as
fontes de áudio vistas nos pacotes. A página manda esse mapa pronto
(`type: mapping`) e o host grava no mesmo formato do Meet. Se o Teams não mandar
o nível de áudio, a presença da fonte no pacote conta como fala (nível 0,2).
O código da sessão é `teams-` + hash do thread da reunião. No texto aparece
`Nome (Teams)` e a evidência é `microsoft_teams_webrtc`. Os objetos internos do
Teams não são documentados: se sumirem, a página não manda mapa e a transcrição
fica com o rótulo do canal. Participante que sai, ou lista indisponível, revoga os
vínculos; reunião nova na mesma aba abre outra sessão.

Prova em reunião real (teams.live.com, 23/09/2026): três participantes com
nome, 14 de 16 falas com o nome certo, 2 recusadas, nenhuma errada. O Teams não
mandou `audioLevel`: cada lote marca `presence`, e a conferência local vira
"presença bate com a gravação" (sem variação no trecho, basta haver som). Limite
que sobra no Teams: aba do Teams silenciada enquanto outro aplicativo toca voz,
com a mesma pessoa falando sem pausa no Teams, não é detectável por esse sinal.

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

Limites: só Google Meet e Teams no navegador, no Chromium; o app desktop do Teams não é coberto; a própria voz não passa pela rede do Meet
(fica no microfone); duas pessoas na mesma sala e no mesmo dispositivo saem com
o nome do dispositivo.
