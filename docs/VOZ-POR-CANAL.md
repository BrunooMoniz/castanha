# Voz por origem, preparação F5V

Risco VERMELHO na ativação: áudio e transcrições pessoais. Não instalado nem
conectado ao daemon nesta entrega. Nenhuma reunião pessoal foi reprocessada.
Nenhum áudio real, nenhum envio e nenhum provedor externo foram executados.

O original dual já contém microfone no canal 0 e sistema no canal 1. Enviar
o estéreo inteiro ao Whisper da Groq perde essa distinção (a API converte
para mono). O novo módulo gera FLAC mono por canal sem outra compressão com
perda, conserva o original e recompõe segmentos pelo tempo, inclusive sobreposições.

Os rótulos são origens técnicas, não identidade biométrica. Microfone pode
conter mais de uma pessoa na sala ou vazamento dos alto-falantes. Sistema
inclui notificações e todos os participantes online juntos. Não atribuir nome
do calendário a um falante sem evidência. Separar pessoas remotas exige diarização
adicional, não entregue por este módulo.

## Linhagem por fala, do canal até o envelope Bronze

Cada fala é um `ChannelUtterance`: além de texto e tempos, carrega `channel`
(0 ou 1), `origin` (`microfone_local` ou `audio_sistema`), o hash do original
e o hash do derivado daquele canal. O nome de falante devolvido pelo provedor é
descartado de propósito: canal é origem, e nome vindo de fora viraria identidade
sem prova. O engine grava essa estrutura em `transcript_segments.json` com
`channels`, `utterance_count` e o hash do job, e repete a origem por gravação
em `metadata.json`. A junção recusa entregar transcrição a menos: se a soma por
canal não bate com o total, ou se alguma fala não aparece no texto final, o
módulo falha em vez de resumir a reunião pela metade.

A ligação disso com a memória (adapter Zinom) ficou de fora desta fatia por
decisão de escopo: é a ponte F4, com outro dono. O `zinom_adapter.py` está
idêntico ao baseline `47ebe06`.

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

Checkpoint subiu para a versão 2 (origem e hash por fala). Checkpoint da
versão anterior é recusado e preservado para inspeção, nunca reinterpretado.

## Prova automatizada

Suíte completa: 238 testes verdes (`python3 -m unittest discover -s tests`),
baseline `47ebe06` tinha 229. Os contratos externos seguem intactos:
`fb2091ad…` e `62614181…`. Nenhum provedor real chamado, nenhum microfone,
áudio sintético gerado por FFmpeg na hora do teste.

Quatorze testes em `tests/test_channel_transcription.py`:

- igualdade PCM entre cada canal original e o FLAC derivado, com o original intacto;
- ordem intercalada com três falas por canal e o último par simultâneo, nenhuma descartada;
- conteúdo integral: toda fala presente no texto final, contagem por canal e hashes conferidos;
- canal em silêncio registrado sem chamar o provedor e sem fala inventada;
- canal com áudio e resposta vazia falha em vez de perder fala, sem criar checkpoint;
- dois canais mudos recusam, e a recusa se repete sem chamar o provedor;
- mock em um canal nunca conclui, e o checkpoint real do outro canal é preservado;
- nome de pessoa devolvido pelo provedor é substituído pela origem do canal;
- retomada após falha do segundo canal sem recobrar o primeiro;
- replay idempotente: mesmo conteúdo, checkpoints byte a byte iguais, provedor não chamado;
- checkpoint corrompido preservado sem nova cobrança;
- timestamps ausentes ou inválidos não geram checkpoint de sucesso;
- mono recusado em vez de alegar duas origens.

Em `tests/test_durable_jobs.py`, prova de ponta a ponta no engine: origem,
canal, tempos e hashes de cada fala chegam ao envelope Bronze, o hash da
gravação vem do job em disco (não do que o provedor afirmou), e só as duas
origens técnicas aparecem como falante.

## Antes de ativar, ainda é obrigatório

- Integrar callback mono com Groq/VPS, MIME FLAC e orçamento por canal. Dois
  canais podem representar até o dobro dos minutos faturados. Não ignorar teto.
- Ponte F4: ligação com o adapter, ingestão com origem e consulta por outro
  cliente. Fora do escopo desta fatia, dono separado.
- Exercitar retomada remota, mistura de canais concluídos e pendentes, e ponte
  integral F4. O chamador deve manter meeting_lock durante a operação.
- Rever checkpoints incompatíveis ao trocar modelo/versão, sem apagar histórico.
  Limite conhecido: a identidade do checkpoint inclui o hash do FLAC derivado, que
  é recriado a cada execução. Se o FFmpeg da máquina mudar de versão entre a
  transcrição e a retomada, o derivado pode sair com hash diferente e o checkpoint
  válido será recusado. Falha segura (nada é apagado nem inventado), mas trava a
  gravação até alguém inspecionar. Resolver na integração, com o hash do PCM.
- Revisão cruzada por outra família, QA real no XPS e instalação reversível
  conforme F5P. Os quatro prompts de enriquecimento já recebem a regra de canal
  versus identidade; isso não é prova comportamental de que um modelo real
  sempre a respeitará.

Reversão: manter versão anterior e áudio original; antes de ativar registrar
os alvos CLI/plugin e estado do daemon conforme INSTALACAO-REVERSIVEL.md. Não
apagar derivados nem checkpoints durante rollback. A nova versão não deve
substituir a transcrição de Nora até completar ambas as origens e a verificação.

Referência: https://console.groq.com/docs/speech-to-text (consultada em 05/09/2026).
