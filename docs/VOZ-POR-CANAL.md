# Voz por origem, preparação F5V

Risco VERMELHO na ativação: áudio e transcrições pessoais. A flag
`transcription.por_canal` vem DESLIGADA e nada foi ativado nesta entrega.
Nenhum áudio real, nenhum envio, nenhum provedor externo chamado, nenhuma
reunião pessoal reprocessada.

O original dual já contém microfone no canal 0 e sistema no canal 1. Enviar
o estéreo inteiro ao Whisper da Groq perde essa distinção (a API converte
para mono). O módulo gera FLAC mono por canal sem outra compressão com
perda, conserva o original e recompõe segmentos pelo tempo, inclusive sobreposições.

Os rótulos são origens técnicas, não identidade biométrica. Microfone pode
conter mais de uma pessoa na sala ou vazamento dos alto-falantes. Sistema
inclui notificações e todos os participantes online juntos. Não atribuir nome
do calendário a um falante sem evidência. Separar pessoas remotas exige diarização
adicional, não entregue por este módulo.

## Ligado ao engine, atrás de flag desligada

`transcription.por_canal` (padrão `false`) escolhe o caminho no engine. Ligada,
os dois pontos de entrada passam a mandar um canal por vez:

- `process_pending`, que é por onde passam a finalização da gravação, o `sync` e
  o daemon;
- `retry_transcription`, o "tentar de novo" do painel. Sem isso ele seria a
  exceção que ainda mandaria o estéreo inteiro para um Whisper só.

Checkpoints ficam em `<bronze>/<slug>/.channels/<hash>/`, dentro do
`meeting_lock`. A VPS ainda NÃO entra por aqui: o job remoto guarda o áudio como
`.ogg` e retoma por esse nome, e mandar FLAC por ali quebraria a retomada de
jobs já aceitos. Enquanto a ponte F4 não cobre isso, o canal fica pendente com o
áudio intacto em vez de subir errado.

## Linhagem por fala, do canal até o envelope Bronze

Cada fala é um `ChannelUtterance`: além de texto e tempos, carrega `channel`
(0 ou 1), `origin` (`microfone_local`, `audio_sistema` ou `gravacao_mono`), o
hash do original e o hash do PCM daquele canal. O nome de falante devolvido pelo
provedor é descartado de propósito: canal é origem, e nome vindo de fora viraria
identidade sem prova. O engine grava a estrutura em `transcript_segments.json`
com `channels`, `utterance_count` e o hash do job, e repete a origem por gravação
em `metadata.json`. A junção recusa entregar transcrição a menos: se a soma por
canal não bate com o total, ou se alguma fala não aparece no texto final, o
módulo falha em vez de resumir a reunião pela metade.

A ligação disso com a memória (adapter Zinom) ficou de fora por decisão de
escopo: é a ponte F4, com outro dono. `zinom_adapter.py`, `sync.py`,
`deployment.py` e `storage.py` seguem idênticos ao baseline `47ebe06`.

## Identidade do canal pelo PCM

O checkpoint (versão 3) identifica o canal pelo hash do áudio DECODIFICADO, não
pelos bytes do FLAC. O derivado é recriado a cada execução: identificar pelo
arquivo fazia uma atualização do FFmpeg recusar um checkpoint válido e obrigar a
pagar o canal de novo. Checkpoint de versão anterior é recusado e preservado
para inspeção, nunca reinterpretado.

## Orçamento e teto por canal

Dois canais são dois envios cobrados. O orçamento é consultado UMA VEZ POR
CANAL, com a duração daquele canal, então o segundo já enxerga o que o primeiro
consumiu. Acima de 3 h por canal, ou sem orçamento, o canal é recusado antes de
qualquer envio: a gravação fica pendente com o áudio intacto, e o canal já
concluído continua no checkpoint, sem nova cobrança na retomada. O FLAC sobe
anunciado como `audio/flac`, e não como Ogg.

## Canal em silêncio

Canal medido como mudo não vai ao provedor. A medição é a mesma função que o
engine usa para classificar `mic_mudo` (`measure_channel_levels`), para que os
dois não discordem e nenhuma fala seja pulada por critério paralelo. O canal
mudo entra no checkpoint com `silent: true`, zero falas e provider
`nenhum (canal em silêncio)`, o outro canal segue normal, e o provider da
reunião continua sendo o real, não `mixed`. Medição indisponível não vira
silêncio presumido: nesse caso o canal vai ao provedor e resposta vazia
continua sendo erro, não fala perdida. Os dois canais mudos são recusa
explícita, nunca sucesso vazio.

## Gravação mono

Gravação com um canal só não é recusada: ela cai para UMA origem técnica
genérica (`gravacao_mono`, rótulo "Áudio da gravação"). Alegar microfone e
sistema onde não existe separação seria afirmar quem falou sem prova. Mais de
dois canais continua recusado.

## Prova automatizada

Suíte completa: 248 testes verdes (`python3 -m unittest discover -s tests`),
baseline `47ebe06` tinha 229. Contratos externos intactos: `fb2091ad…` e
`62614181…`. Nenhum provedor real chamado, nenhum microfone, áudio sintético
gerado por FFmpeg na hora do teste.

Dezenove testes em `tests/test_channel_transcription.py`:

- igualdade PCM entre cada canal original e o FLAC derivado, com o original intacto;
- identidade pelo PCM: outro FFmpeg gera outros bytes, o checkpoint continua válido;
- ordem intercalada com três falas por canal e o último par simultâneo, nenhuma descartada;
- conteúdo integral: toda fala no texto final, contagem por canal e hashes conferidos;
- duração informada por canal ao chamador;
- teto por canal recusado antes de chegar ao provedor;
- orçamento consultado por canal, e a recusa do segundo preserva o primeiro pago;
- canal em silêncio registrado sem chamar o provedor e sem fala inventada;
- canal com áudio e resposta vazia falha em vez de perder fala, sem criar checkpoint;
- dois canais mudos recusam, e a recusa se repete sem chamar o provedor;
- mock em um canal nunca conclui, e o checkpoint real do outro é preservado;
- nome de pessoa devolvido pelo provedor é substituído pela origem do canal;
- retomada após falha do segundo canal sem recobrar o primeiro;
- replay idempotente: mesmo conteúdo, checkpoints byte a byte iguais, provedor não chamado;
- checkpoint corrompido preservado sem nova cobrança;
- timestamps ausentes ou inválidos não geram checkpoint de sucesso;
- mono cai para origem genérica; mais de dois canais recusado.

Em `tests/test_durable_jobs.py`, prova no engine: flag desligada por padrão
manda a gravação inteira como antes; ligada, cada canal vai sozinho em FLAC, a
queda do canal remoto deixa a reunião pendente com o canal 0 já no checkpoint,
e a retomada envia SÓ o canal que faltava, fechando o envelope com as duas
origens; só a VPS disponível deixa o áudio pendente em vez de subir FLAC com
nome de Ogg; e origem, canal, tempos e hashes de cada fala chegam ao envelope
Bronze com o hash vindo do job em disco.

Em `tests/test_transcription.py`, o FLAC sobe como `audio/flac`.

## Antes de ativar, ainda é obrigatório

- Ponte F4: ligação com o adapter, ingestão com origem e consulta por outro
  cliente. Fora do escopo desta fatia, dono separado.
- Cobrir a VPS no caminho por canal, sem quebrar a retomada de job remoto já aceito.
- Exercitar retomada remota e ponte integral F4 com áudio real. O chamador deve
  manter `meeting_lock` durante a operação.
- Revisão cruzada por outra família no SHA final, QA real no XPS e instalação
  reversível conforme F5P. Os quatro prompts de enriquecimento já recebem a regra
  de canal versus identidade; isso não é prova comportamental de que um modelo
  real sempre a respeitará.

Reversão: a flag desligada já devolve o comportamento anterior sem tocar em
arquivo. Além dela, manter versão anterior e áudio original; antes de ativar
registrar os alvos CLI/plugin e estado do daemon conforme INSTALACAO-REVERSIVEL.md.
Não apagar derivados nem checkpoints durante rollback. A nova versão não deve
substituir a transcrição de Nora até completar ambas as origens e a verificação.

Referência: https://console.groq.com/docs/speech-to-text (consultada em 05/09/2026).
