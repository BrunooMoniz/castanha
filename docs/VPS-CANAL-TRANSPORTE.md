# Transporte de canal para a VPS

Risco **VERMELHO na operação**: ativar envia áudio à VPS configurada. Esta entrega
é um delta local não instalável, sem instalação, serviço ASR, rede real de áudio
ou conteúdo pessoal. A flag `transcription.por_canal` continua desligada.

## Seleção e retomada

O provedor padrão é `vps_ssh`, mesmo com chave e orçamento Groq disponíveis.
Groq é secundária, com `groq_fallback_mode="manual"`. Pending, timeout ou erro da
VPS não disparam Groq. Mock nunca é fallback; no modo por canal até seleção
mock explícita é recusada pelo validador real.

Antes de qualquer transporte por canal, a seleção pública é persistida em
`.providers/<sha-original>.json` e no job Bronze como `selected_provider` e
`provider_selection`. Não inclui credencial. A mesma seleção serve todos os
canais e tentativas da gravação, independentemente de oscilação do orçamento.
Modelo, host, idioma/configuração e contrato fazem parte do checkpoint.

Para mudar deliberadamente de VPS para Groq, a configuração exige
`provider="groq"`, `provider_revision` maior que a revisão persistida e
`fallback_from="vps_ssh"`. Uma revisão nova preserva a seleção anterior e usa
`.channels/revision-N`, retranscrevendo todos os canais. Ela não aproveita um
canal VPS junto de outro Groq. Uma gravação já concluída tem o job anterior
arquivado antes de ser reprocessada por revisão explícita.

O canal só produz checkpoint após validação real de provedor, texto completo,
segmentos, tempos e origem. Timestamps ausentes permanecem ausentes e são
recusados, sem zeros inventados. Falha deixa original e job recuperáveis, sem
avançar Silver, Gold ou Zinom. A origem continua técnica e genérica; o callback
usa `mode="mic_only"`.

## Contrato remoto v2, sem instalação nesta fatia

O worker versionável é `scripts/castanha-transcribe-v2.py`, instalado
como `/root/castanha-transcribe-v2.py` pelo rollout verificado. Reaproveita o
job SSH/nohup durável e usa um flock global antes de carregar o modelo.
Não há serviço ou banco novo: só uma inferência ocupa memória/CPU por vez.
O tradeoff é recarregar o modelo por job e aguardar o lock dentro do timeout;
se expirar, o original e o pedido continuam disponíveis para retomada.

`--describe-contract` deve devolver JSON estrito, sem campos extras ou duplicados:

```json
{
  "contract": "faster-whisper-json-v2",
  "model": "large-v3",
  "compute_type": "int8",
  "cpu_threads": 8,
  "multilingual": true,
  "condition_on_previous_text": false,
  "chunk_length": 30,
  "segmentation_strategy": "whisper-vad-v1"
}
```

O modelo opera offline, com detecção de idioma por janela, sem tradução,
VAD com silêncio mínimo de 500 ms e janelas de 30 s. Não segmenta por frase
curta, estratégia que produziu falsas detecções de idioma no ensaio.
O ensaio real na VPS preservou português e inglês, mas omitiu uma frase na
troca de idioma: isto não equivale a precisão humana garantida. O original
é a evidência canônica para conferir números, nomes e decisões importantes.

O mapeamento de configuração é `transcription.vps_worker_contract` para
`contract`, e `transcription.vps_<campo>` para os demais. `config.example.json`
e `castanha/config.py` registram os valores fornecidos pela coordenação. O
cliente só envia FLAC/lança o worker após igualdade exata da atestação.
Ausência, JSON inválido ou incompatibilidade resultam em `TranscriptionPending`.

`--request <caminho>` recebe arquivo regular privado `request.json` (0600),
publicado atomicamente no diretório estável do job. Manifesto:

- `namespace="flac-mono-v1"`, `mode="mic_only"`, `pcm_sha256`;
- `contract`: objeto completo atestado;
- `request_sha256`: identidade derivada de PCM, modo, namespace e contrato;
- `audio`: caminho do FLAC, `mime_type="audio/flac"`, `sample_rate=16000`,
  `channels=1`.

A resposta precisa conter `request_sha256` e `contract` correspondentes, além
de `text` e `segments` com `text/start/end`. Não há áudio nem segredo no argv
do worker, somente o caminho do manifesto. Resultado inválido fica preservado
para inspeção. O cliente não apaga um resultado para fabricar sucesso.

## Áudio e durabilidade

FLAC exige assinatura verdadeira, codec FLAC, um canal e duração positiva.
A decodificação local recusa corrupção antes do transporte. FLAC de 16 kHz é
enviado intacto; outras taxas geram um FLAC mono de 16 kHz em `.vps-transport`,
com duração validada, sem codec com perda e sem alterar o original. O diretório
não é inventariado como uma segunda gravação. A identidade usa PCM validado,
portanto recompressão do mesmo conteúdo não cria outro job remoto.

O transporte Ogg legado preserva bytes, extensão, nomes, hash e consulta/replay
originais. Resultado concluído é lido sem conversão nem reescrita. Execução nova
ou retry do worker antigo ficam `pending_worker_contract`, sem SCP ou lançamento,
até F5W atestar suporte explícito a Ogg original no v2. Compatibilidade do
armazenamento não autoriza usar o ASR antigo.
SCP reserva 15 s de conexão mais o tempo para transferir o arquivo a 256 KiB/s,
com piso de 30 s e teto de 30 min; cada SSH continua limitado a 15 s. Isso evita
abortar sistematicamente os FLACs de reuniões longas. Timeout e saída não zero
removem somente o temporário exclusivo daquela tentativa; falha da limpeza
também aparece como pendência. Originais, áudio já publicado, resultados e
temporários antigos não são apagados. `nohup` libera a conexão; `flock` evita
execuções simultâneas do mesmo job. O timeout do worker mantém a faixa de
30 minutos a 3 horas. Timeout preserva áudio, manifesto e job para retomada.

## Provas e limitações

`test_vps_channel_transport.py` e `test_vps_channel_callback.py` executam
FFmpeg/ffprobe, shell, nohup/flock/timeout reais com áudio sintético e SSH/SCP
interceptados. Cobrem atestação, manifesto 0600, resposta perdida, um upload
por job, recompressão equivalente, revisão sem mistura, validador, corrupção,
silêncio e timeout. Reconhecimento Whisper não foi executado nesta fatia.

Medições externas informadas pela coordenação (não repetidas neste worker):
fixture PT→EN de 58,69 s levou 56,18 s e pico RSS de 3054 MB com large-v3 CPU,
int8/8 threads. A janela de 15 s prejudicou bordas; a de 30 s levou 77,06 s e
RSS de 3053 MB, mas omitiu a frase PT de transição. Detectar PT e EN não aprovou
code-switch. Não se promete GPU, qualidade humana nem execução instalável.

Revisão: sessões Codex frescas de review e skeptic sob waiver explícito,
**mesma família do implementador**, sem alegar família distinta.

## Composição e reversão

A base `db9d156` não contém `bf7b927` (zinomNeedsSync/zinomIcon, 15 casos QML e
skipped por credencial). O integrador final comporá `bf7b927`/F5SF + F5C/F5VH +
ponte `57c1e27` + F5PT2 e dependerá do worker F5W aprovado. UI e ponte não foram
integradas nesta worktree.

Reversão registrada antes de qualquer ativação: voltar ao código anterior,
manter `por_canal` desligado e preservar originais, derivados, seleções,
revisões, checkpoints e jobs. O legado não deve drenar jobs FLAC como Ogg.
Nenhum deploy faz parte desta entrega.
